from __future__ import annotations

import gspread

from config import AppConfig


class GoogleSheetsClient:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.client = gspread.service_account(filename=config.google_service_account_file)

    def open_by_url(self, sheet_url: str):
        return self.client.open_by_url(sheet_url)

    def create(self, title: str):
        return self.client.create(title)
