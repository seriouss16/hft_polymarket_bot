"""Tests for order parameter validation and secret masking utilities."""

import math
import pytest
from core.live_engine import LiveExecutionEngine
from utils.secrets_mask import mask_api_key, mask_address


class TestValidateOrderParams:
    """Test suite for _validate_order_params static method."""

    def test_valid_buy_order(self):
        """Test valid BUY order parameters."""
        is_valid, error_msg = LiveExecutionEngine._validate_order_params("BUY", 0.50, 10.0)
        assert is_valid is True
        assert error_msg == ""

    def test_valid_sell_order(self):
        """Test valid SELL order parameters."""
        is_valid, error_msg = LiveExecutionEngine._validate_order_params("SELL", 0.75, 5.0)
        assert is_valid is True
        assert error_msg == ""

    def test_nan_price(self):
        """Test rejection of NaN price."""
        is_valid, error_msg = LiveExecutionEngine._validate_order_params("BUY", float("nan"), 10.0)
        assert is_valid is False
        assert "price is not finite" in error_msg

    def test_nan_size(self):
        """Test rejection of NaN size."""
        is_valid, error_msg = LiveExecutionEngine._validate_order_params("SELL", 0.50, float("nan"))
        assert is_valid is False
        assert "size is not finite" in error_msg

    def test_infinity_price(self):
        """Test rejection of infinite price."""
        is_valid, error_msg = LiveExecutionEngine._validate_order_params("BUY", float("inf"), 10.0)
        assert is_valid is False
        assert "price is not finite" in error_msg

    def test_negative_infinity_size(self):
        """Test rejection of negative infinity size."""
        is_valid, error_msg = LiveExecutionEngine._validate_order_params("SELL", 0.50, float("-inf"))
        assert is_valid is False
        assert "size is not finite" in error_msg

    def test_zero_price(self):
        """Test rejection of zero price."""
        is_valid, error_msg = LiveExecutionEngine._validate_order_params("BUY", 0.0, 10.0)
        assert is_valid is False
        assert "price must be > 0" in error_msg

    def test_negative_price(self):
        """Test rejection of negative price."""
        is_valid, error_msg = LiveExecutionEngine._validate_order_params("SELL", -0.25, 5.0)
        assert is_valid is False
        assert "price must be > 0" in error_msg

    def test_zero_size(self):
        """Test rejection of zero size."""
        is_valid, error_msg = LiveExecutionEngine._validate_order_params("BUY", 0.50, 0.0)
        assert is_valid is False
        assert "size must be > 0" in error_msg

    def test_negative_size(self):
        """Test rejection of negative size."""
        is_valid, error_msg = LiveExecutionEngine._validate_order_params("SELL", 0.75, -1.0)
        assert is_valid is False
        assert "size must be > 0" in error_msg

    def test_price_above_one(self):
        """Test rejection of price > 1.0 for Polymarket."""
        is_valid, error_msg = LiveExecutionEngine._validate_order_params("BUY", 1.5, 10.0)
        assert is_valid is False
        assert "price must be <= 1.0" in error_msg

    def test_price_exactly_one(self):
        """Test acceptance of price exactly 1.0."""
        is_valid, error_msg = LiveExecutionEngine._validate_order_params("SELL", 1.0, 5.0)
        assert is_valid is True
        assert error_msg == ""

    def test_invalid_side(self):
        """Test rejection of invalid side."""
        is_valid, error_msg = LiveExecutionEngine._validate_order_params("HOLD", 0.50, 10.0)
        assert is_valid is False
        assert "invalid side" in error_msg

    def test_all_corner_cases_combined(self):
        """Test multiple invalid parameters (first error should be caught first)."""
        # Order of checks: finite(price), finite(size), price>0, size>0, price<=1, side
        is_valid, error_msg = LiveExecutionEngine._validate_order_params("BUY", float("nan"), float("nan"))
        assert is_valid is False
        assert "price is not finite" in error_msg


class TestMaskApiKey:
    """Test suite for mask_api_key function."""

    def test_mask_normal_api_key(self):
        """Test masking a typical API key."""
        api_key = "pk_1234567890abcdef"
        masked = mask_api_key(api_key)
        assert masked == "...cdef"

    def test_mask_short_api_key(self):
        """Test masking a short API key (<=4 chars) returns as-is."""
        api_key = "abcd"
        masked = mask_api_key(api_key)
        assert masked == "abcd"

    def test_mask_very_short_api_key(self):
        """Test masking a very short API key."""
        api_key = "abc"
        masked = mask_api_key(api_key)
        assert masked == "abc"

    def test_mask_none_api_key(self):
        """Test masking None returns placeholder."""
        masked = mask_api_key(None)
        assert masked == "????"

    def test_mask_empty_api_key(self):
        """Test masking empty string returns placeholder."""
        masked = mask_api_key("")
        assert masked == "????"

    def test_mask_exactly_4_chars(self):
        """Test masking exactly 4 characters returns as-is."""
        api_key = "1234"
        masked = mask_api_key(api_key)
        assert masked == "1234"

    def test_mask_5_chars(self):
        """Test masking 5 characters shows last 4."""
        api_key = "12345"
        masked = mask_api_key(api_key)
        assert masked == "...2345"


class TestMaskAddress:
    """Test suite for mask_address function."""

    def test_mask_ethereum_address(self):
        """Test masking a typical Ethereum address."""
        address = "0x1234567890abcdef1234567890abcdef12345678"
        masked = mask_address(address)
        assert masked == "...5678"

    def test_mask_short_address(self):
        """Test masking a short address (<=8 chars) returns as-is."""
        address = "0x1234"
        masked = mask_address(address)
        assert masked == "0x1234"

    def test_mask_none_address(self):
        """Test masking None returns placeholder."""
        masked = mask_address(None)
        assert masked == "????"

    def test_mask_empty_address(self):
        """Test masking empty string returns placeholder."""
        masked = mask_address("")
        assert masked == "????"

    def test_mask_exactly_8_chars(self):
        """Test masking exactly 8 characters (e.g., 0x + 6 hex) returns as-is."""
        address = "0xabcdef"
        masked = mask_address(address)
        assert masked == "0xabcdef"

    def test_mask_9_chars(self):
        """Test masking 9 characters shows last 4."""
        address = "0x1234567"
        masked = mask_address(address)
        assert masked == "...4567"
