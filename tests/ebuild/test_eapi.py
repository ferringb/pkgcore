import shutil
from unittest import mock

import pytest
from pkgcore.const import EBD_PATH
from pkgcore.ebuild import eapi as eapi_mod
from pkgcore.ebuild.eapi import EAPI, get_eapi


# Seperate these so we can test the fixture implementation.
def _protect_eapi_registration():
    """Protect EAPI.known_eapi so any test manipulations can't persist"""
    prior = EAPI.known_eapis.copy()
    yield
    EAPI.known_eapis.clear()
    EAPI.known_eapis.update(prior)


protect_eapi_registration = pytest.fixture(scope="function", autouse=True)(
    _protect_eapi_registration
)


def test_eapi_registry_fixture():
    prior = set(list(EAPI.known_eapis))
    fixture = _protect_eapi_registration()
    # start the fixture
    next(fixture)
    assert prior == set(EAPI.known_eapis), EAPI.known_eapis
    # known_eapis is a weakval dict, thus we have to hold the reference
    _x = EAPI.register("foon")
    assert len(EAPI.known_eapis) - 1 == len(prior)
    # finish the fixture via exausting the iterator
    list(fixture)
    assert set(EAPI.known_eapis) == prior


def test_get_eapi():
    # unknown EAPI
    unknown_eapi = get_eapi("unknown")
    assert unknown_eapi in EAPI.unknown_eapis.values()
    # check that unknown EAPI is now registered as an unknown
    assert unknown_eapi == get_eapi("unknown")

    # known EAPI
    assert get_eapi("6") == eapi_mod.eapi6


def test_get_PMS_eapi():
    # test PMS filtration
    assert get_eapi("6") is eapi_mod.eapi6
    # hold the reference, known_eapis is weakval
    temp = EAPI.register("1234")
    # confirm it's visable
    assert get_eapi("1234", suppress_unsupported=False) is temp
    assert eapi_mod.get_PMS_eapi("1234") is None
    temp2 = EAPI.register("9999", pms=True)
    assert eapi_mod.get_PMS_eapi("9999") is temp2


def test_get_PMS_eapis():
    pms_eapis = set(eapi_mod.get_PMS_eapis())
    expected = set(x for x in EAPI.known_eapis.values() if x.pms)
    assert pms_eapis == expected


def test_get_latest_pms_eapi():
    # if it's not in there, the magic constant isn't in alignment
    assert eapi_mod.get_latest_PMS_eapi() in list(eapi_mod.get_PMS_eapis())

    # Note, this can false positive while a new  EAPI is being developed.  If this
    # is an actual issue, then introduce a flag on EAPI objects that indicates 'latest pms',
    # and update get_latest_PMS_eapi to scan for that, and register() to block duplicate.
    assert (
        eapi_mod.get_latest_PMS_eapi()
        is sorted(
            (x for x in EAPI.known_eapis.values() if x.pms),
            key=lambda e: int(e._magic),
            reverse=True,
        )[0]
    )


class TestEAPI:
    def test_pms_default_off(self):
        assert EAPI("asdf").pms == False
        assert EAPI.register("asdf").pms == False

    def test_register(self, tmp_path):
        # re-register known EAPI
        with pytest.raises(ValueError):
            EAPI.register(magic="0")

        mock_ebd_temp = str(shutil.copytree(EBD_PATH, tmp_path / "ebd"))
        with (
            mock.patch("pkgcore.ebuild.eapi.bash_version") as bash_version,
            mock.patch.dict(eapi_mod.EAPI.known_eapis),
            mock.patch("pkgcore.ebuild.eapi.const.EBD_PATH", mock_ebd_temp),
        ):
            # inadequate bash version
            bash_version.return_value = "3.1"
            with pytest.raises(SystemExit) as excinfo:
                new_eapi = EAPI.register(magic="new", optionals={"bash_compat": "3.2"})
            assert (
                "EAPI 'new' requires >=bash-3.2, system version: 3.1"
                == excinfo.value.args[0]
            )

            # adequate system bash versions
            bash_version.return_value = "3.2"
            test_eapi = EAPI.register(magic="test", optionals={"bash_compat": "3.2"})
            assert test_eapi._magic == "test"
            bash_version.return_value = "4.2"
            test_eapi = EAPI.register(magic="test1", optionals={"bash_compat": "4.1"})
            assert test_eapi._magic == "test1"

    def test_is_supported(self, tmp_path, caplog):
        assert eapi_mod.eapi6.is_supported

        mock_ebd_temp = str(shutil.copytree(EBD_PATH, tmp_path / "ebd"))
        with (
            mock.patch.dict(eapi_mod.EAPI.known_eapis),
            mock.patch("pkgcore.ebuild.eapi.const.EBD_PATH", mock_ebd_temp),
        ):
            # partially supported EAPI is flagged as such
            test_eapi = EAPI.register("test", optionals={"is_supported": False})
            assert test_eapi.is_supported
            assert caplog.text.endswith("EAPI 'test' isn't fully supported\n")

            # unsupported/unknown EAPI is flagged as such
            unknown_eapi = get_eapi("blah")
            assert not unknown_eapi.is_supported

    def test_inherits(self):
        for eapi_str, eapi_obj in EAPI.known_eapis.items():
            objs = (get_eapi(str(x)) for x in range(int(eapi_str), -1, -1))
            assert list(map(str, eapi_obj.inherits)) == list(map(str, objs))

    def test_ebd_env(self):
        for eapi_str, eapi_obj in EAPI.known_eapis.items():
            assert eapi_obj.ebd_env["EAPI"] == eapi_str
