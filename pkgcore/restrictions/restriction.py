# Copyright: 2005 Brian Harring <ferringb@gmail.com>
# License: GPL2

"""
base restriction class
"""

from pkgcore.util import caching
from pkgcore.util.compatibility import any
from pkgcore.util.currying import pre_curry, pretty_docs

class base(object):

    """
    base restriction matching object.

    all derivatives *should* be __slot__ based (lot of instances may
    wind up in memory).
    """

    __metaclass__ = caching.WeakInstMeta
    __inst_caching__ = True

    # __weakref__ here's is implicit via the metaclass
    __slots__ = ("negate",)
    package_matching = False

    def __init__(self, negate=False):
        """
        @param negate: should the match results be negated?
        """
        self.negate = negate

#	def __setattr__(self, name, value):
#		import traceback;traceback.print_stack()
#		object.__setattr__(self, name, value)
#		try:	getattr(self, name)
#
#		except AttributeError:
#			object.__setattr__(self, name, value)
#		else:	raise AttributeError

    def match(self, *arg, **kwargs):
        raise NotImplementedError

    def force_False(self, *arg, **kwargs):
        return not self.match(*arg, **kwargs)

    def force_True(self, *arg, **kwargs):
        return self.match(*arg, **kwargs)

    def intersect(self, other):
        return None

    def __len__(self):
        return 1

    def __repr__(self):
        return str(self)

    def __str__(self):
        # without this __repr__ recurses...
        raise NotImplementedError


class AlwaysBool(base):
    """
    restriction that always yields a specific boolean
    """
    __slots__ = ("type",)

    __inst_caching__ = True

    def __init__(self, node_type=None, negate=False):
        """
        @param node_type: the restriction type the instance should be,
            typically L{pkgcore.restrictions.packages.package_type} or
            L{pkgcore.restrictions.values.value_type}
        @param negate: boolean to return for the match
        """
        base.__init__(self, negate=negate)
        self.type = node_type

    def match(self, *a, **kw):
        return self.negate

    def force_True(self, *a, **kw):
        return self.negate

    def force_False(self, *a, **kw):
        return not self.negate

    def __iter__(self):
        return iter([])

    def __str__(self):
        return "always '%s'" % self.negate


class Negate(base):

    """
    wrap and negate a restriction instance
    """

    __slots__ = ("type", "_restrict")
    __inst_caching__ = False

    def __init__(self, restrict):
        """
        @param restrict: L{pkgcore.restrictions.restriction.base} instance
            to negate
        """
        self.type = restrict.type
        self._restrict = restrict

    def match(self, *a, **kw):
        return not self._restrict.match(*a, **kw)

    def __str__(self):
        return "not (%s)" % self._restrict


class FakeType(base):

    """
    wrapper to wrap and fake a node_type
    """

    __slots__ = ("type", "_restrict")
    __inst_caching__ = False

    def __init__(self, restrict, new_type):
        """
        @param restrict: L{pkgcore.restrictions.restriction.base} instance
            to wrap
        @param new_type: new node_type
        """
        self.type = new_type
        self._restrict = restrict

    def match(self, *a, **kw):
        return self._restrict.match(*a, **kw)

    def __str__(self):
        return "Faked type(%s): %s" % (self.type, self._restrict)


class AnyMatch(base):

    """Apply a nested restriction to every item in a sequence."""

    __slots__ = ('restriction', 'type')

    def __init__(self, childrestriction, node_type, negate=False):
        """Initialize.

        @type  childrestriction: restriction
        @param childrestriction: child restriction applied to every value.
        @type  restriction_type: string
        @param restriction_type: type of this restriction.
        """
        base.__init__(self, negate)
        self.restriction, self.type = childrestriction, node_type

    def match(self, val):
        return any(self.restriction.match(x) for x in val) != self.negate

    def __str__(self):
        return "any: %s match" % (self.restriction,)

    def __repr__(self):
        return '<%s restriction=%r @%#8x>' % (
            self.__class__.__name__, self.restriction, id(self))


def curry_node_type(klass, node_type, extradoc=None):
    """Helper function for creating restrictions of a certain type.

    This uses pre_curry to pass a node_type to the wrapped class,
    and extends the docstring.

    @param klass: callable (usually a class) that is wrapped.
    @param node_type: value passed as node_type.
    @param extradoc: addition to the docstring. Defaults to
        "Automatically set to %s type." % node_type

    @return: a wrapped callable.
    """
    if extradoc is None:
        extradoc = "Automatically set to %s type." % (node_type,)
    doc = klass.__doc__
    result = pre_curry(klass, node_type=node_type)
    if doc is None:
        doc = ''
    else:
        # do this so indentation on pydoc __doc__ is sane
        doc = "\n".join(line.lstrip() for line in doc.split("\n")) + "\n"
        doc += extradoc
    return pretty_docs(result, doc)


value_type = "values"
package_type = "package"
