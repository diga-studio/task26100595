import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from peopledesk import PeopleDesk  # noqa: E402


@pytest.fixture()
def desk(tmp_path):
    instance = PeopleDesk(str(tmp_path / "leave.db"))
    yield instance
    instance.close()
