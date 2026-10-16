import pytest

from siigo_bridge.__main__ import main


@pytest.mark.parametrize("arguments", [
    ["login"],
    ["login", "--machine", "not-a-machine"],
])
def test_login_rejects_missing_or_invalid_machine_identity(arguments):
    with pytest.raises(SystemExit) as error:
        main(arguments)
    assert error.value.code == 2
