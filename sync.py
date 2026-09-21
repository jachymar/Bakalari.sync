import os
import json
import hashlib
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build


BAK_URL = os.getenv(
    "BAK_URL",
    "https://gyarab.bakalari.cz",
).rstrip("/")

BASE_URL = f"{BAK_URL}/api/3"


def get_bakalari_token(user, password):
    response = requests.post(
        f"{BAK_URL}/api/login",
        data={
            "client_id": "ANDR",
            "grant_type": "password",
            "username": user,
            "password": password,
        },
        timeout=30,
    )
    response.raise_for_status()

    response_data = response.json()
    return response_data["access_token"]
