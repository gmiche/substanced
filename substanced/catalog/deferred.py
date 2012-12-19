import sys
import functools
import logging
import persistent
import threading
import time
import transaction

from transaction.interfaces import ISavepointDataManager

from zope.interface import implementer

from ZODB.POSException import ConflictError
from ZODB.ConflictResolution import PersistentReference

from pyramid.threadlocal import get_current_registry

from substanced.interfaces import (
    IIndexingActionProcessor,
    MODE_DEFERRED,
    )

from ..util import get_oid

logger = logging.getLogger(__name__)

# functools.total_ordering allows us to define __eq__ and __lt__ and it takes
# care of the rest of the rich comparison methods (2.7+ only)

@functools.total_ordering
class Action(object):

    resource = None
    oid = None
    index = None
    position = None

    def __repr__(self):
        klass = self.__class__
        classname = '%s.%s' % (klass.__module__, klass.__name__)
        return '<%s object oid %r for index %r at %#x>' % (
            classname,
            self.oid,
            getattr(self.index, '__name__', None),
            id(self)
            )

    def __hash__(self):
        # We don't actually need to supply a unique hash value to allow
        # instances of this class to be unique according to set membership
        # (dict membership); __eq__ will be called for each.
        return 1

    def __eq__(self, other):
        # In the case this is called during conflict resolution, self.index and
        # self.resource will be instances of
        # ZODB.ConflictResolution.PersistentReference; we have to wrap them in
        # proxies if so; see PersistentReferenceProxy docstring for rationale.
        self_cmp = (pr_wrap(self.index), self.oid, pr_wrap(self.resource))
        other_cmp = (pr_wrap(other.index), other.oid, pr_wrap(other.resource))
        return self_cmp == other_cmp

    def __lt__(self, other):
        # This is used by optimize_actions, but must not be called during
        # conflict resolution because self.index and other.index will be
        # persistent references instead of normal persistent objects
        self_cmp = (self.oid, get_oid(self.index, None), self.position)
        other_cmp = (other.oid, get_oid(other.index, None), other.position)
        return self_cmp < other_cmp

def pr_wrap(obj):
    if isinstance(obj, PersistentReference):
        return PersistentReferenceProxy(obj)
    return obj

class PersistentReferenceProxy(object):
    """PersistentReferenceProxy

    `ZODB.ConflictResolution.PersistentReference` doesn't get handled correctly
    in the __eq__ method due to lack of the `__hash__` method.
    So we make workaround here to utilize `__cmp__` method of
    `PersistentReference`.

    """
    def __init__(self, pr):
        self.pr = pr

    def __hash__(self):
        return 1

    def __eq__(self, other):
        try:
            return self.pr == other.pr
        except ValueError:
            return False

    def __ne__(self, other):
        return not self.__eq__(other)

class IndexAction(Action):

    position = 2

    def __init__(self, index, mode, oid, resource):
        self.index = index
        self.mode = mode
        self.oid = oid
        self.resource = resource

    def execute(self):
        self.index.index_doc(self.oid, self.resource)
        # break all refs
        self.index = None
        self.oid = None
        self.resource = None

class ReindexAction(Action):

    position = 1
    
    def __init__(self, index, mode, oid, resource):
        self.index = index
        self.mode = mode
        self.oid = oid
        self.resource = resource

    def execute(self):
        self.index.reindex_doc(self.oid, self.resource)
        # break all refs
        self.index = None
        self.oid = None
        self.resource = None

class UnindexAction(Action):

    position = 0
    
    def __init__(self, index, mode, oid):
        self.index = index
        self.mode = mode
        self.oid = oid

    def execute(self):
        self.index.unindex_doc(self.oid)
        # break all refs
        self.index = None
        self.oid = None

class ActionsQueue(persistent.Persistent):

    logger = logger # for testing

    def __init__(self):
        self.gen = 0
        self.actions = []
        self.processor_active = False

    def bumpgen(self):
        # At an average rate of 100 bumps per second, this value won't exceed
        # sys.maxint for:
        #
        # About 8 months on a 32-bit system.
        #
        # About 92 years on a 64-bit system.
        #
        # The software will work fine after it exceeds sys.maxint, it'll
        # overflow to a long integer and it'll just be slower to do the math to
        # bump.
        #
        # I choose to not care about the slowdown that overflowing to a long
        # integer on incredibly busy 32-bit systems will imply.  If someone
        # uses this software 100 years from now, and they're still using a 64
        # bit CPU, they'll also need to deal with the slowdown implied by
        # overflowing to a long integer too.
        #
        # It is possible to reset this value to 0 periodically.  Resetting this
        # value to 0 should can be done immediately after a pack.  It is only
        # used to compare old object revisions to newer ones, and after a pack,
        # there are no old object revisions anymore.  It would be ideal to be
        # able to hook a pack and do this automatically, but meh.
        newgen = self.gen + 1
        self.gen = newgen

    def extend(self, actions):
        self.actions.extend(actions)
        self.bumpgen()
        self._p_changed = True

    def popall(self):
        if not self.actions:
            return None
        actions = self.actions[:]
        self.actions = []
        self.bumpgen()
        return actions

    def _p_resolveConflict(self, old_state, committed_state, new_state):
        # Most of this code is cadged from zc.queue._queue; the undo logic
        # comes from from Products.QueueCatalog
        
        # We only know how to merge actions and resolve the generation.  If
        # anything else is different, puke.
        if set(new_state.keys()) != set(committed_state.keys()):
            raise ConflictError

        for key, val in new_state.items():
            if ( key not in ('actions', 'gen') and
                 val != committed_state.get(key) ):
                raise ConflictError  # cant deal w/ processor_active diff either

        processor_active = new_state['processor_active']

        new_gen = new_state['gen']
        old_gen = old_state['gen']
        committed_gen = committed_state['gen']

        # If the new state's generation number is less than the old state's
        # generation number, we know we're in the process of undoing a
        # transaction that involved the queue.  This is because during an undo
        # the "new_state" state is the state of the queue in the transaction
        # prior to the undone transaction (the state that would have been
        # rolled back to if this conflict did not occur), and the "old_state"
        # is actually the state of the transaction we're trying to undo.

        undoing = (old_gen > new_gen)

        if undoing:
            # Brain too small to cope with properly supporting the undo case
            # despite ~4 hours of intense trying; must support soon, but have
            # to move on (which chaps my ass something fierce).  Findings:
            #
            # While we're undoing, it's not enough to just depend on set state
            # differences.  Instead we need to add anti-actions for every
            # action we'll end up removing.  For example, if we determine via
            # conflict resolution that an UnindexAction was in the old state
            # but will be removed from the state we'll return, we'll need to
            # add an IndexAction to the returned state in order to get the
            # object into the indexed state eventually (when the queue
            # processor runs).
            #
            # I got as far as adding this bit of code:
            #
            #         if undoing:
            #             mod_committed.extend(
            #                 [a.anti() for a in new_removed])
            #
            # Where a.anti() generates an anti-action for the removed action.
            # That code isn't even right though, because it doesnt preserve the
            # ordering of removed actions.  Also not sure if we have to
            # generate anti-actions for any *added* actions.  Also don't know
            # what value to return for a generation value when coping with an
            # undo situation.  Brain too small, time too short.
            #
            raise ConflictError

        gen = max(committed_gen, new_gen)

        old = old_state['actions']
        committed = committed_state['actions']
        new = new_state['actions']

        old_set = set(old)
        committed_set = set(committed)
        new_set = set(new)

        committed_added = committed_set - old_set
        committed_removed = old_set - committed_set
        new_added = new_set - old_set
        new_removed = old_set - new_set

        if new_removed & committed_removed:
            # they both removed (claimed) the same one.  Puke.
            raise ConflictError  # can't resolve

        elif new_added & committed_added:
            # they both added the same one.  Puke.
            raise ConflictError  # can't resolve

        # Merge into the committed state and return it.
        mod_committed = []

        for action in committed:
            if not action in new_removed:
                mod_committed.append(action)

        if new_added:
            ordered_new_added = new[-len(new_added):]
            assert set(ordered_new_added) == new_added
            mod_committed.extend(ordered_new_added)

        committed_state['actions'] = mod_committed
        committed_state['gen'] = gen
        committed_state['processor_active'] = processor_active

        self.logger.info('resolved %s conflict' % self.__class__.__name__)

        return committed_state

def commit(tries):
    def wrapper(wrapped):
        def retry(self, *arg, **kw):
            for _ in range(tries):
                self.sync()
                self.transaction.begin()
                try:
                    result = wrapped(self, *arg, **kw)
                    self.transaction.commit()
                    return result
                except ConflictError:
                    self.transaction.abort()
            raise ConflictError
        return retry
    return wrapper

class Break(Exception):
    pass

class BasicActionProcessor(object):

    logger = logger # for testing
    transaction = transaction # for testing
    queue_name = 'basic_action_queue'
    
    def __init__(self, context):
        self.context = context

    def get_root(self):
        jar = self.context._p_jar
        if jar is None:
            return None
        zodb_root = jar.root()
        return zodb_root

    def get_queue(self):
        zodb_root = self.get_root()
        if zodb_root is None:
            return None
        queue = zodb_root.get(self.queue_name)
        return queue

    def active(self):
        queue = self.get_queue()
        if queue is None:
            return False
        return queue.processor_active

    def sync(self):
        jar = self.context._p_jar
        if jar is not None:
            jar.sync()

    @commit(5)
    def engage(self):
        queue = self.get_queue()
        if queue is None:
            zodb_root = self.get_root()
            if zodb_root is None:
                raise RuntimeError('Context has no jar')
            queue = ActionsQueue()
            queue.processor_active = True
            zodb_root[self.queue_name] = queue
        else:
            queue.processor_active = True

    @commit(1)
    def disengage(self):
        queue = self.get_queue()
        if queue is not None:
            queue.processor_active = False

    def add(self, actions):
        queue = self.get_queue()
        if queue is None:
            raise RuntimeError('Queue processor not engaged')
        queue.extend(actions)

    def process(self, sleep=5, once=False):
        self.logger.info('starting basic action processor')
        self.engage()
        while True:
            try:
                if not once: # pragma: no cover
                    time.sleep(sleep)
                self.logger.info('start running actions processing')
                self.sync()
                self.transaction.begin()
                executed = False
                queue = self.get_queue()
                actions = queue.popall()
                if actions is not None:
                    actions = optimize_actions(actions)
                    for action in actions:
                        self.logger.info('executing %s' % (action,))
                        action.execute()
                        executed = True
                if executed:
                    self.logger.info('committing')
                    try:
                        self.transaction.commit()
                        self.logger.info('committed')
                    except ConflictError:
                        self.transaction.abort()
                        self.logger.info('aborted due to conflict error')
                else:
                    self.logger.info('no actions to execute')
                self.logger.info('end running actions processing')
                if once:
                    raise Break()
            except (SystemExit, KeyboardInterrupt, Break):
                once = True
                try:
                    self.logger.info('stopping basic action processor')
                    self.disengage()
                    break
                except ConflictError:
                    self.logger.info(
                        'couldnt disengage due to conflict, processing queue '
                        'once more'
                        )

class IndexActionSavepoint(object):
    """ Transaction savepoints  """

    def __init__(self, tm):
        self.tm = tm
        self.actions = tm.actions[:]

    def rollback(self):
        self.tm.actions = self.actions


@implementer(ISavepointDataManager)
class IndexActionTM(threading.local):
    # This is a data manager solely to provide savepoint support, we'd
    # otherwise be able to get away with just using a before commit hook to
    # call .process

    transaction = transaction # for testing
    logger = logger # for testing
    
    def __init__(self, index):
        self.index = index
        self.oid = index.__oid__
        self.registered = False
        self.actions = []

    def register(self):
        if not self.registered:
            t = self.transaction.get()
            t.join(self)
            t.addBeforeCommitHook(self.flush, (False,))
            self.registered = True

    def savepoint(self):
        return IndexActionSavepoint(self)

    def tpc_begin(self, t):
        pass

    commit = tpc_vote = tpc_begin

    def tpc_finish(self, t):
        self.registered = False
        self.actions = []
        if self.index is not None:
            self.index.clear_action_tm()
            self.index = None # break circref

    tpc_abort = abort = tpc_finish

    def sortKey(self):
        return self.oid

    def add(self, action):
        self.actions.append(action)

    def flush(self, all=True):
        if self.actions:
            actions = self.actions
            self.actions = []
            actions = optimize_actions(actions)
            self._process(actions, all=all)

    def _process(self, actions, all=True):
        registry = get_current_registry()

        self.logger.debug('begin index actions processing')

        if all:
            self.logger.debug('executing all actions immediately: "all" flag')
            self.execute_actions_immediately(actions)
            
        else:
            processor = registry.queryAdapter(
                self.index,
                IIndexingActionProcessor
                )

            active = False
            reason = None

            if processor:
                active = processor.active()
                if not active:
                    reason = 'inactive'
            else:
                reason = 'no'

            if active:
                self.logger.debug(
                    'executing deferred actions: action processor active'
                    )
                self.execute_actions_deferred(actions, processor)
            else:
                self.logger.debug(
                    'executing actions all immediately: %s action '
                    'processor' % (reason,)
                    )
                self.execute_actions_immediately(actions)

        self.logger.debug('done processing index actions')

    def execute_actions_immediately(self, actions):
        for action in actions:
            self.logger.debug('executing action %r' % (action,))
            action.execute()

    def execute_actions_deferred(self, actions, processor):
        deferred = []
        for action in actions:
            if action.mode is MODE_DEFERRED:
                self.logger.debug('adding deferred action %r' % (action,))
                deferred.append(action)
            else:
                self.logger.debug('executing action %r' % (action,))
                action.execute()
        if deferred:
            processor.add(deferred)

def optimize_actions(actions):
    """
    State chart for optimization.  If the new action is X and the existing
    action is Y, generate the resulting action named in the chart cells.

                            New    INDEX    UNINDEX   REINDEX

       Existing    INDEX           index     nothing*   index*

                 UNINDEX           reindex*  unindex    reindex

                 REINDEX           index     unindex    reindex

    Starred entries in the chart above indicate special cases.  Typically
    the last action encountered in the actions list is the most optimal
    action, except for the starred cases.
    """
    result = {}

    def donothing(oid, index_oid, action1, action2):
        del result[(oid, index_oid)]

    def doadd(oid, index_oid, action1, action2):
        result[(oid, index_oid)] = action1

    def dochange(oid, index_oid, action1, action2):
        result[(oid, index_oid)] = ReindexAction(
            action2.index, action2.mode, oid, action2.resource
            )

    def dodefault(oid, index_oid, action1, action2):
        result[(oid, index_oid)] = action2

    statefuncs = {
        # txn asked to remove an object that previously it was
        # asked to add, conclusion is to do nothing
        (IndexAction, UnindexAction):donothing,
        # txn asked to change an object that was not previously added,
        # concusion is to just do the add
        (IndexAction, ReindexAction):doadd,
        # txn action asked to remove an object then readd the same
        # object.  We translate this to a single change action.
        (UnindexAction, IndexAction):dochange,
        }

    for newaction in actions:
        oid = newaction.oid
        index_oid = newaction.index.__oid__
        oldaction = result.get((oid, index_oid))
        statefunc = statefuncs.get(
            (oldaction.__class__, newaction.__class__),
            dodefault,
            )
        statefunc(oid, index_oid, oldaction, newaction)

    result = list(sorted(result.values()))
    return result
