"""Inventory Checker Agent: phone-camera counting and verification against an items to be tested listing, on MAF.

Headless use::

    from inventory_checker import InventoryCheck
    check = InventoryCheck.from_file("items_to_be_tested.xlsx")
    check.check_paths(["photos/"])
    check.export("results.xlsx")
"""

__version__ = "0.5.0"

from .engine import InventoryCheck

__all__ = ["InventoryCheck", "__version__"]
