#!/usr/bin/env python3
"""Test script for median average calculation."""

import sys
from pathlib import Path

# Add hft_bot to path
sys.path.insert(0, str(Path(__file__).parent))

from utils.stats import _median_avg


def test_median_avg():
    """Test the median average calculation."""
    test_cases = [
        # (input_list, expected_result, description)
        ([1, 2, 3, 4, 5, 6, 7], 4.0, "Odd n=7: (3+4+5)/3 = 4"),
        ([1, 2, 3], 2.0, "Odd n=3: (1+2+3)/3 = 2"),
        ([1, 2], 1.5, "Even n=2 (too small): mean = 1.5"),
        ([1, 2, 3, 4], 2.5, "Even n=4: (2+3)/2 = 2.5"),
        ([1, 2, 3, 4, 5, 6], 3.5, "Even n=6: (3+4)/2 = 3.5"),
        ([1, 2, 3, 4, 5, 6, 7, 8], 4.5, "Even n=8: (4+5)/2 = 4.5"),
        ([10], 10.0, "Single element: mean = 10"),
        ([], 0.0, "Empty list: 0.0"),
        ([5, 5, 5, 5], 5.0, "All same values: 5"),
        ([1, 1, 2, 2, 3, 3, 4, 4], 2.5, "Even with duplicates: (2+3)/2 = 2.5"),
        ([-3, -2, -1, 0, 1, 2, 3], 0.0, "Symmetric odd: median avg = 0"),
        ([-2, -1, 0, 1], -0.5, "Symmetric even: (-1+0)/2 = -0.5"),
    ]

    all_passed = True
    for values, expected, description in test_cases:
        result = _median_avg(values)
        passed = abs(result - expected) < 1e-9
        status = "✓" if passed else "✗"
        print(f"{status} {description}")
        print(f"  Input: {values}")
        print(f"  Expected: {expected}, Got: {result}")
        if not passed:
            all_passed = False
        print()

    if all_passed:
        print("All tests passed! ✓")
        return 0
    else:
        print("Some tests failed! ✗")
        return 1


if __name__ == "__main__":
    sys.exit(test_median_avg())
