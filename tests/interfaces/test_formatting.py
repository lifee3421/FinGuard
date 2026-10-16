from dataclasses import dataclass

from finguard.interfaces.formatting import to_jsonable, view_field


@dataclass
class Item:
    amount: str


def test_views_accept_dataclasses_and_mappings():
    item = Item("12.30")
    assert to_jsonable(item) == {"amount": "12.30"}
    assert view_field(item, "amount") == "12.30"
    assert view_field({"amount": "9.10"}, "amount") == "9.10"
