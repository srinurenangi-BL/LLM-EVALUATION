import datetime
import email.utils
import time
import urllib.request

def apply_timezone_patch() -> None:
    """Detects if the local system clock has drifted from Google's server time
    (e.g., due to local timezone or VM state drift) and monkeypatches
    google.auth._helpers.utcnow to correct it.
    """
    try:
        # Fetch headers from google.com
        req = urllib.request.Request("https://www.google.com", method="HEAD")
        with urllib.request.urlopen(req, timeout=5) as response:
            date_str = response.headers.get("Date")
            if not date_str:
                return
            
            # Parse RFC 2822 date from Google header
            google_time_parsed = email.utils.parsedate_to_datetime(date_str)
            google_utc_timestamp = google_time_parsed.timestamp()
            
            # Get local time in UTC/timestamp
            local_timestamp = time.time()
            
            # Offset in seconds
            offset = google_utc_timestamp - local_timestamp
            
            # Apply patch if drift is more than 10 seconds
            if abs(offset) > 10:
                import google.auth._helpers
                original_utcnow = google.auth._helpers.utcnow
                google.auth._helpers.utcnow = lambda: original_utcnow() + datetime.timedelta(seconds=offset)
                print(f"[Time Patcher] Adjusted clock skew by {offset:.2f} seconds (~{offset/3600:.2f} hours)")
    except Exception as exc:
        print(f"[Time Patcher] Warning: Could not apply timezone patch: {exc}")
