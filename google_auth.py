from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

CREDENTIALS_PATH = Path(__file__).parent / "credentials.json"
TOKEN_PATH = Path(__file__).parent / "token.json"

SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/gmail.readonly",
]


def get_credentials() -> Credentials:
    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())

    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
        creds = flow.run_local_server(port=0, open_browser=False)

    TOKEN_PATH.write_text(creds.to_json())
    return creds


if __name__ == "__main__":
    credentials = get_credentials()
    print(f"Authenticated. Token saved to {TOKEN_PATH}")
