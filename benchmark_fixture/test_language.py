import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app import auth
import time
from unittest.mock import patch

# Test all language-related functionality
def test_language_default():
    """Test default language is zh-CN"""
    base_time = 1000000.0
    with patch('app.auth.time.time', return_value=base_time):
        s = auth.create_session("applicant")
        assert s["language"] == "zh-CN", f"Expected zh-CN, got {s['language']}"
        print("✓ test_language_default passed")

def test_language_custom():
    """Test custom language can be set"""
    base_time = 1100000.0
    with patch('app.auth.time.time', return_value=base_time):
        s = auth.create_session("applicant", language="en-US")
        assert s["language"] == "en-US", f"Expected en-US, got {s['language']}"
        
        s = auth.create_session("reviewer", language="ja-JP")
        assert s["language"] == "ja-JP", f"Expected ja-JP, got {s['language']}"
        print("✓ test_language_custom passed")

def test_get_language():
    """Test get_language function"""
    base_time = 1200000.0
    with patch('app.auth.time.time', return_value=base_time):
        s = auth.create_session("applicant")
        session_dict = {"token": s["token"]}
        assert auth.get_language(session_dict) == "zh-CN"
        
        s = auth.create_session("reviewer", language="en-US")
        session_dict = {"token": s["token"]}
        assert auth.get_language(session_dict) == "en-US"
        print("✓ test_get_language passed")

def test_get_language_not_logged_in():
    """Test get_language returns None for invalid sessions"""
    assert auth.get_language(None) is None
    assert auth.get_language({"token": "invalid"}) is None
    print("✓ test_get_language_not_logged_in passed")

def test_refresh_preserves_language():
    """Test refresh_token preserves language"""
    base_time = 1300000.0
    with patch('app.auth.time.time', return_value=base_time):
        s = auth.create_session("applicant", language="fr-FR")
        token = s["token"]
        
        # Refresh using session dict with only token
        refreshed = auth.refresh_token({"token": token})
        assert refreshed["language"] == "fr-FR", f"Expected fr-FR, got {refreshed['language']}"
        print("✓ test_refresh_preserves_language passed")

if __name__ == "__main__":
    test_language_default()
    test_language_custom()
    test_get_language()
    test_get_language_not_logged_in()
    test_refresh_preserves_language()
    print("\n=== All language tests passed! ===")
