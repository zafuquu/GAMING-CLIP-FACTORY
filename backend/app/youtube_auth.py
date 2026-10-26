"""Run python -m app.youtube_auth from the backend directory to connect a YouTube channel."""
import os
from pathlib import Path
import keyring
from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow
from .integrations.youtube import SERVICE_NAME, CREDENTIALS_KEY, CREDENTIALS_SCOPE_VERSION_KEY, CREDENTIALS_SCOPE_VERSION, SCOPES

def main():
    load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)
    client_file = os.getenv("GCF_YOUTUBE_CLIENT_SECRETS", "").strip()
    if not client_file:
        raise SystemExit("Set GCF_YOUTUBE_CLIENT_SECRETS to the OAuth desktop client JSON downloaded from Google Cloud Console. Do not commit that file.")
    path = Path(client_file).expanduser().resolve()
    if not path.is_file():
        raise SystemExit(f"OAuth client file not found: {path}")
    print("Opening a browser for the owner to authorize the intended YouTube channel.")
    print("Tokens will be stored through the operating system keyring, not in the repository.")
    flow = InstalledAppFlow.from_client_secrets_file(str(path), scopes=SCOPES)
    credentials = flow.run_local_server(host="127.0.0.1", port=0, open_browser=True, access_type="offline", prompt="consent")
    granted=getattr(credentials,"granted_scopes",None)
    effective=set(granted or getattr(credentials,"scopes",[]) or [])
    missing=[scope for scope in SCOPES if scope not in effective]
    if missing:
        raise SystemExit("The OAuth result did not include every required YouTube scope. Recheck the Google Cloud OAuth consent/configuration, then reconnect.")
    keyring.set_password(SERVICE_NAME, CREDENTIALS_KEY, credentials.to_json())
    keyring.set_password(SERVICE_NAME, CREDENTIALS_SCOPE_VERSION_KEY, CREDENTIALS_SCOPE_VERSION)
    print("YouTube OAuth credentials saved to the OS keyring. No token was printed.")
    print("Return to the dashboard and check YouTube connection status.")

if __name__ == "__main__":
    main()
