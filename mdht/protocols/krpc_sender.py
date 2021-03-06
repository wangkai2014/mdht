import random
from collections import defaultdict

from zope.interface import implements, Interface
from twisted.python import log
from twisted.internet import reactor, defer, protocol
from twisted.python.components import proxyForInterface
from twisted.internet.interfaces import IUDPTransport

from mdht import constants, contact
from mdht.coding import krpc_coder
from mdht.coding.krpc_coder import InvalidKRPCError
from mdht.kademlia import routing_table
from mdht.krpc_types import Query, Response, Error
from mdht.transaction import Transaction
from mdht.protocols.errors import TimeoutError, KRPCError 

class KRPC_Sender(protocol.DatagramProtocol):
    def __init__(self, routing_table_class, node_id, _reactor=None):
        # If the user doesn't specify a reactor, we will use
        # one from twisted.internet
        if _reactor is None:
            self._reactor = reactor
        self.node_id = long(node_id)
        self._transactions = dict()
        self.routing_table = routing_table_class(self.node_id)
        # TODO rework the routing table classes: are multiple needed?, maybe
        # one interface, one implementation, to leave room for the potential
        # of making a direct-to-database implementation later?

    def datagramReceived(self, data, address):
        """
        This method is called by twisted when a datagram is received

        This implementation tries to decode the datagram. If it succeeds,
        it is passed onto self.krpcReceived for further processing, otherwise
        the encoding exception is captured and logged

        @see krpcReceived

        """
        try:
            krpc = krpc_coder.decode(data)
        except InvalidKRPCError:
            log.msg("{0}:{1} sent a malformed packet"
                .format(address[0], address[1]))
            return
        self.krpcReceived(krpc, address)

    def krpcReceived(self, krpc, address):
        if isinstance(krpc, Query):
            self.queryReceived(krpc, address)
        else:
            transaction = self._transactions.get(krpc._transaction_id, None)
            if transaction is not None:
                if isinstance(krpc, Response):
                    self.responseReceived(krpc, transaction, address)
                elif isinstance(krpc, Error):
                    self.errorReceived(krpc, transaction, address)
            else:
                log.msg("Received a reply not corresponding to an" +
                        " outstanding query from: %s, reply: %s" % (
                        contact.address_str(address), str(krpc)))

    def queryReceived(self, query, address):
        method_name = "%s_Received" % query.rpctype
        dispatcher = getattr(self, method_name, None)
        if dispatcher is not None:
            dispatcher(query, address)

    def responseReceived(self, response, transaction, address):
        transaction.deferred.callback(response)

    def errorReceived(self, error, transaction, address):
        transaction.deferred.errback(KRPCError(error))

    def sendKRPC(self, krpc, address):
        encoded_packet = krpc_coder.encode(krpc)
        self.transport.write(encoded_packet, address)

    def sendQuery(self, query, address, timeout):
        query._from = self.node_id
        query._transaction_id = self._generate_transaction_id()
        try:
            self.sendKRPC(query, address)
        except InvalidKRPCError as encoding_error:
            return defer.fail(encoding_error)

        t = Transaction()
        t.query = query
        t.address = address
        t.deferred = defer.Deferred()
        t.deferred.addCallback(self._query_success, address, t)
        t.deferred.addErrback(self._query_failure, address, t)
        t.timeout_call = self._reactor.callLater(
            constants.rpctimeout, t.deferred.errback, TimeoutError())
        self._transactions[query._transaction_id] = t
        t.deferred.addBoth(self._remove_transaction, t)
        return t.deferred

    def sendResponse(self, response, address):
        response._from = self.node_id
        self.sendKRPC(response, address)

    def sendError(self, error, address):
        self.sendKRPC(error, address)

    def _query_success(self, response, address, transaction):
        """
        Handle a valid Response to an outstanding Query

        This callback records changes to the statistics for the
        node behind the address/response (ie, it updates its RTT
        and makes sures it is in the routing table)

        """
        # Pull the node corresponding to this response out
        # of our routing table, or create it if it doesn't exist
        response_node = self.routing_table.get_node(response._from)
        if response_node is None:
            response_node = contact.Node(response._from, address)
        response_node.successful_query(transaction.time)
        self.routing_table.offer_node(response_node)
        # Pass the response further down the callback chain
        return response

    def _query_failure(self, failure, address, transaction):
        """
        Handle exceptions encountered while waiting for a Response

        This errback processes TimeoutErrors and KRPCErrors.
        Specifically, it updates the statistics of the node
        responsible for the exception (if it can be found),
        and removes it from the routing table if necessary

        """
        # Only enter this code block if the error
        # is either a TimeoutError or a KRPCError
        f = failure.trap(TimeoutError, KRPCError)

        errornodes = self.routing_table.get_node_by_address(address)
        if errornodes is None:
            return failure

        for errornode in errornodes:
            if f == TimeoutError:
                # TODO multi-factor eviction (freshness is good,
                # but what about (ie) number of failed queries?)
                if not errornode.fresh():
                    self.routing_table.remove_node(errornode)
            elif f == KRPCError:
                errornode.failed_query(transaction.time)

        return failure

    def _remove_transaction(self, result, transaction):
        """
        Callback/errback that removes an outstanding transaction

        The corresponding timeout delayed call is also cancelled
        if it has not yet been called

        """
        transaction_id = transaction.query._transaction_id
        if transaction_id in self._transactions:
                del self._transactions[transaction_id]

        if transaction.timeout_call.active():
            transaction.timeout_call.cancel()

        return result

    def _generate_transaction_id(self):
        """
        Generate a transaction_id unique to our transaction table

        @see mdht.constants.transaction_id_size
        @returns a unique transaction_id of constants.transaction_id_size size

        """
        while True:
            transaction_id = random.getrandbits(constants.transaction_id_size)
            if transaction_id not in self._transactions:
                return transaction_id
