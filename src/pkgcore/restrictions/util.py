"""
restriction related utilities
"""

from functools import partial

from snakeoil.sequences import iflatten_func

from pkgcore.restrictions import packages, boolean, restriction

def _restriction_type_filter(desired_type, inst):
    return getattr(inst, "type", None) == desired_type \
        and not isinstance(inst, boolean.base)

def visit_restrictions(desired_type, restrict, attrs=None, invert=False):
    """Visit and descend through a restrict for the given restriction type.

    :param restrict: package instance to scan
    :param attrs: None (return all package restrictions), or a sequence of
        specific attrs the package restriction must work against.
    """
    if not isinstance(restrict, (list, tuple)):
        restrict = [restrict]
    for r in restrict:
        if not isinstance(r, restriction.base):
            raise TypeError(
                'restrict must be of a restriction.base, '
                f'not {r.__class__.__class__}: {r!r}'
            )

    i = iflatten_func(restrict, partial(_restriction_type_filter, desired_type))
    if attrs is None:
        return i
    attrs = frozenset(attrs)
    return (
        r for r in i
        if invert == attrs.isdisjoint(getattr(r, 'attrs', ()))
    )

collect_package_restrictions = partial(visit_restrictions, restriction.package_type)
