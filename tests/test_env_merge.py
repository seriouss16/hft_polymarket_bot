"""Tests for utils.env_merge."""

from utils.env_merge import strip_env_inline_comment


def test_strip_env_inline_comment_trailing_hash():
    assert strip_env_inline_comment("0.047  # было 0.045") == "0.047"
    assert strip_env_inline_comment("5.2  # было 5.5") == "5.2"


def test_strip_env_inline_comment_no_space_before_hash_unchanged():
    assert strip_env_inline_comment("foo#bar") == "foo#bar"


def test_strip_env_inline_comment_quoted_hash_preserved():
    assert strip_env_inline_comment('"0.05 # not a comment"') == '"0.05 # not a comment"'
