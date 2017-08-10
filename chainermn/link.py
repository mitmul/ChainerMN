from six.moves import queue

import chainer
import chainermn
import chainermn.communicators
import chainermn.functions


class MultiNodeChainList(chainer.ChainList):
    """Combining multiple non-connected components of computational graph.

    This class combines each ``chainer.Chain``, which represents one of the
    non-connected component in compuational graph. In ``__call__()``,
    the returned object of ``chainer.Chain`` (which represents pointer)
    are passed to the next ``chainer.Chain``, in order to retain the
    computational graph connected and make backprop work properly.

    Users add each ``chainer.Chain`` by ``add_link()`` method. Each chain
    is invoked in forward computation according to the order they are added,
    and in backward computation according to the reversed order.

    .. admonition:: Example

        This is a simple example of the model which sends its outputs to
        rank=1 machine::

            import chainer
            import chainer.functions as F
            import chainermn


            class SimpleModelSub(chainer.Chain):

                def __init__(self, n_in, n_hidden, n_out):
                    super(SimpleModelSub, self).__init__(
                        l1=L.Linear(n_in, n_hidden),
                        l2=L.Linear(n_hidden, n_out))

                def __call__(self, x):
                    h1 = F.relu(self.l1(x))
                    return self.l2(h1)


            class SimpleModel(chainermn.MultiNodeChainList):

                def __init__(self, comm, n_in, n_hidden, n_out):
                    super(SimpleModel, self).__init__(comm)
                    self.add_link(
                        SimpleModelSub(n_in, n_hidden, n_out),
                        rank_in=None,
                        rank_out=1)

    .. admonition:: Example

        This is the other example of two models interacting each other::

            import chainer
            import chainer.functions as F
            import chainermn


            class MLP(chainer.Chain):

                def __init__(self, n_in, n_hidden, n_out):
                    super(MLP, self).__init__(
                        l1=L.Linear(n_in, n_hidden),
                        l2=L.Linear(n_hidden, n_hidden),
                        l3=L.Linear(n_hidden, n_out))

                def __call__(self, x):
                    h1 = F.relu(self.l1(x))
                    h2 = F.relu(self.l2(h1))
                    return self.l3(h2)


            class Model0(chainermn.MultiNodeChainList):

                def __init__(self, comm):
                    super(Model0, self).__init__(comm)
                    self.add_link(
                        MLP(10000, 5000, 2000),
                        rank_in=None,
                        rank_out=1)
                    self.add_link(
                        MLP(100, 50, 10),
                        rank_in=1,
                        rank_out=None)


            class Model1(chainermn.MultiNodeChainList):

                def __init__(self, comm):
                    super(Model1, self).__init__(comm)
                    self.add_link(MLP(2000, 500, 100), rank_in=0, rank_out=0)


        ``Model0`` is expected to be on rank=0, and ``Model1`` is expected to
        be on rank=1. The first ``MLP`` in ``Model0`` will send its outputs
        to ``Model1``, then ``MLP`` in ``Model1`` will receive it and send
        its outputs to the second ``MLP`` in ``Model0``.

    Args:
        comm (chainermn.communicators._base.CommunicatorBase):
            ChainerMN communicator.
    """

    def __init__(self, comm):
        chainer.utils.experimental('chainermn.MultiNodeChainList')
        super(MultiNodeChainList, self).__init__()
        self._comm = comm
        self._rank_inouts = []

    def add_link(self, link, rank_in=None, rank_out=None):
        """Register one connected link with its inout rank.

        Args:
            link (chainer.Link): The link object to be registered.
            rank_in (int or list):
                Ranks from which it receives data. If None is specified,
                the model does not receive from any machines.
            rank_out (int or list):
                Ranks to which it sends data. If None is specified,
                the model will not send to any machine.
        """
        super(MultiNodeChainList, self).add_link(link)
        if isinstance(rank_in, int):
            rank_in = [rank_in]
        if isinstance(rank_out, int):
            rank_out = [rank_out]

        if rank_out is None:
            for _, _rank_out in self._rank_inouts:
                if _rank_out is None:
                    raise ValueError(
                        'MultiNodeChainList cannot have more than two '
                        'computational graph component whose rank_out is None')

        self._rank_inouts.append((rank_in, rank_out))

    def __call__(self, *inputs):
        comm_queue = queue.Queue()
        y = None
        delegate_variable = None

        for i_comp, (f, (rank_in, rank_out)) in \
                enumerate(zip(self._children, self._rank_inouts)):
            x = None

            if rank_in is None:  # Use inputs.
                if i_comp == 0:
                    x = f(*inputs)
                else:
                    # If the graph component is not the first one,
                    # backprop to the previous graph component must be
                    # guaranteed.
                    x = chainermn.functions.pseudo_connect(
                        delegate_variable,
                        *inputs)
                    x = f(x)

            else:  # Receive inputs from the other machines.
                # Preprocess: receiving inputs from the other machines.
                xs = []
                for _rank_in in rank_in:
                    if _rank_in == self._comm.rank:
                        # Receive inputs from itself.
                        if delegate_variable is None:
                            _x = comm_queue.get()
                        else:
                            _x = chainermn.functions.pseudo_connect(
                                delegate_variable,
                                comm_queue.get())
                    else:
                        _x = chainermn.functions.recv(
                            self._comm,
                            rank=_rank_in,
                            delegate_variable=delegate_variable,
                            device=self._device_id)

                    xs.append(_x)

                    # Guarantee the backward path to the previous graph
                    # component to be executed in the last to avoid dead-lock.
                    delegate_variable = _x

                # Guarantee backprop on the same edge exactly once.
                delegate_variable = None

                # Actual forward.
                x = f(*tuple(xs))

            if rank_out is None:  # Return outputs.
                assert y is None, "MultiNodeChainList cannot have more than "\
                    "two computational graph component whose rank_out is None"
                y = x  # model output
                delegate_variable = y

            else:  # Send outputs to the other machines.
                for i_comp, _rank_out in enumerate(rank_out):
                    if _rank_out == self._comm.rank:
                        # Send outputs to itself.
                        if delegate_variable is not None:
                            x = chainermn.functions.pseudo_connect(
                                delegate_variable,
                                x)
                        comm_queue.put(x)
                        delegate_variable = x
                    elif i_comp == 0:
                        delegate_variable = chainermn.functions.send(
                            x, self._comm,
                            rank=_rank_out)
                    else:
                        # If the model has multiple targets for send,
                        # we must guarantee backwards of each send to be
                        # called in the reversed order.
                        if delegate_variable is not None:
                            x = chainermn.functions.pseudo_connect(
                                delegate_variable,
                                x)
                        delegate_variable = chainermn.functions.send(
                            x, self._comm,
                            rank=_rank_out)

        assert comm_queue.empty()

        # Return.
        if y is delegate_variable:
            # The last computational graph component returns model output.
            return y
        elif y is not None:
            # The intermediate graph component returns model output.
            return chainermn.functions.pseudo_connect(delegate_variable, y)
        else:
            # Do not have any model output.
            return delegate_variable
