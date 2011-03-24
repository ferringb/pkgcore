# Copyright: 2005-2009 Brian Harring <ferringb@gmail.com>
# Copyright: 2005 Marien Zwart <marienz@gentoo.org>
# License: BSD/GPL2

# potentially use an intermediate base for user config errors,
# seperate base for instantiation?


"""Exceptions raised by the config code."""

__all__ = ("BaseError", "TypeDefinitionError", "ConfigurationError", "ParsingError",
    "CollapseInheritOnly", "InstantiationError", "QuoteInterpretationError"
)

from snakeoil.demandload import demandload
demandload(globals(),
    "snakeoil.currying:pretty_docs,post_curry",
)

class BaseError(Exception):
    pass


class TypeDefinitionError(BaseError):
    """Fatal error in type construction."""


class ConfigurationError(BaseError):

    """Fatal error in parsing a config section.

    :type stack: sequence of strings.
    @ivar stack: messages describing where this ConfigurationError originated.
        configuration-related code catching ConfigurationError that wants to
        raise its own ConfigurationError should modify (usually append to)
        the stack and then re-raise the original exception (this makes sure
        the traceback is preserved).
    """

    def __init__(self, message):
        BaseError.__init__(self, message)
        self.stack = [message]

    def __str__(self):
        return ':\n'.join(reversed(self.stack))


class ParsingError(ConfigurationError):

    def __init__(self, message=None, exception=None):
        if message is not None:
            ConfigurationError.__init__(self, message)
        elif exception is not None:
            ConfigurationError.__init__(self, str(exception))
        else:
            raise ValueError('specify at least one of message and exception')
        self.message = message
        self.exc = exception

    def __str__(self):
        return "Parsing Failed: %s\n%s" % (self.message, self.exc)

    @classmethod
    def wrap_exception(cls, message):
        return post_curry(cls._inner_wrap_exception, message)

    @classmethod
    def _inner_wrap_exception(cls, functor, message):

        def f(*args, **kwargs):
            try:
                return functor(*args, **kwargs)
            except (RuntimeError, SystemExit, KeyboardInterrupt):
                raise
            except Exception, e:
                if isinstance(e, BaseError):
                    raise
                raise cls(message=message, exception=e)
        f.func = functor
        f.__name__ = functor.__name__
        return pretty_docs(f)


class CollapseInheritOnly(ConfigurationError):
    """Attempt was made to collapse an uncollapsable section.

    Separate exception because pconfig catches it separately.
    """


class InstantiationError(ConfigurationError):

    """Exception occured during instantiation.

    @ivar callable: callable object which failed during instantiation.
    @ivar pargs: positional args passed to callable.
    @ivar kwargs: keyword args passed to callable.
    @ivar exc: Original exception object or None.

    A well-behaved configurable callable should raise this exception
    if instantiation failed, providing one or both of message and
    exception. The other fields will be filled in by central.

    If the callable raises something else central will wrap it in
    this, but that will lose the traceback.
    """

    def __init__(self, message=None, exception=None, callable_obj=None,
                 pargs=None, kwargs=None):
        if message is not None:
            ConfigurationError.__init__(self, message)
        elif exception is not None:
            ConfigurationError.__init__(self, str(exception))
        else:
            raise ValueError('specify at least one of message and exception')
        self.message = message
        self.callable = callable_obj
        self.pargs = pargs
        self.kwargs = kwargs
        self.exc = exception

    def __str__(self):
        # self.callable should be set here (nothing should try to catch
        # and str() this before central had a chance to fill it in)
        if self.message is not None:
            if self.callable is None:
                message = '%r, callable unset!' % (self.message,)
            else:
                message = '%r instantiating %s.%s' % (
                    self.message, self.callable.__module__,
                    self.callable.__name__)
        # The weird repr(str(exc)) used here quotes the message nicely.
        elif self.callable is not None:
            message = "Caught exception %r instantiating %s.%s" % (
                str(self.exc), self.callable.__module__,
                self.callable.__name__)
        else:
            message = "Caught exception %r, callable unset!" % (str(self.exc),)
        return ':\n'.join(reversed([message] + self.stack[1:]))


class QuoteInterpretationError(ConfigurationError):

    """Quoting of a var was screwed up."""

    def __init__(self, string):
        ConfigurationError.__init__(self, "Parsing of %r failed" % (string,))
        self.str = string
