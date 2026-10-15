"""Grant only declared delegated UI permissions in the deployment's tenant."""

import httpx
from azure.identity import AzureCliCredential

from .setup_toolbox import grant_delegated_consent, terraform_output


def main() -> None:
    tenant = terraform_output("tenant_id")
    with AzureCliCredential(tenant_id=tenant) as credential, httpx.Client(timeout=60) as client:
        for name in ("ui_api_client_id", "ui_browser_client_id"):
            grant_delegated_consent(terraform_output(name), credential, client)
    print("UI delegated administrator consent is configured.")


if __name__ == "__main__":
    main()
