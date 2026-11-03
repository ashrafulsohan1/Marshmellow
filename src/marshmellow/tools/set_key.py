"""Store the BYOK cloud API key in Windows Credential Manager (D7).

Usage: uv run python -m marshmellow.tools.set_key
The prompt hides input; the key never lands in shell history, TOML, or logs.
"""

from __future__ import annotations

import getpass

from marshmellow.core.secrets import store_api_key


def main() -> None:
    key = getpass.getpass("Cloud API key (input hidden): ").strip()
    if not key:
        print("No key entered — nothing stored.")
        return
    store_api_key(key)
    print("Stored in Windows Credential Manager (service 'Marshmellow').")
    print("Set [providers.llm] base_url and model in config.toml to point at your provider.")


if __name__ == "__main__":
    main()
