import asyncio
from decimal import Decimal, InvalidOperation
from typing import Dict, List

from fastapi import APIRouter, Depends, HTTPException, Query
from starlette import status

from deps import get_accounts_service, get_bots_orchestrator
from models import GatewayWalletCredential, SetDefaultWalletRequest
from services.accounts_service import AccountsService, validate_safe_name
from services.bots_orchestrator import BotsOrchestrator

router = APIRouter(tags=["Accounts"], prefix="/accounts")

# 钱包栏发起的授权不一定关联某个 Bot。总账仍需要稳定的归属键，
# 但前端会把这两个内部值显示为“钱包授权”，不会伪装成 Bot 名称。
WALLET_AUTHORIZATION_BOT_NAME = "__wallet_authorization__"
WALLET_AUTHORIZATION_CONTROLLER_ID = "__wallet_authorization__"


def _is_real_transaction_hash(value: object) -> bool:
    """Gateway 在无需链上交易时会返回全零哈希，不能把它记为账单。"""
    transaction_hash = str(value or "").strip().lower()
    return transaction_hash.startswith("0x") and len(transaction_hash) == 66 and set(transaction_hash[2:]) != {"0"}


@router.get("/", response_model=List[str])
async def list_accounts(accounts_service: AccountsService = Depends(get_accounts_service)):
    """
    Get a list of all account names in the system.

    Returns:
        List of account names
    """
    return accounts_service.list_accounts()


@router.get("/{account_name}/credentials", response_model=List[str])
async def list_account_credentials(account_name: str,
                                   accounts_service: AccountsService = Depends(get_accounts_service)):
    """
    Get a list of all connectors that have credentials configured for a specific account.

    Args:
        account_name: Name of the account to list credentials for

    Returns:
        List of connector names that have credentials configured

    Raises:
        HTTPException: 404 if account not found
    """
    try:
        credentials = accounts_service.list_credentials(account_name)
        # Remove .yml extension from filenames
        return [cred.replace('.yml', '') for cred in credentials]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/add-account", status_code=status.HTTP_201_CREATED)
async def add_account(account_name: str, accounts_service: AccountsService = Depends(get_accounts_service)):
    """
    Create a new account with default configuration files.

    Args:
        account_name: Name of the new account to create

    Returns:
        Success message when account is created

    Raises:
        HTTPException: 400 if account already exists or the account name is invalid
    """
    validate_safe_name(account_name, "account name")
    try:
        accounts_service.add_account(account_name)
        return {"message": "Account added successfully."}
    except FileExistsError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/delete-account")
async def delete_account(account_name: str, accounts_service: AccountsService = Depends(get_accounts_service)):
    """
    Delete an account and all its associated credentials.

    Args:
        account_name: Name of the account to delete

    Returns:
        Success message when account is deleted

    Raises:
        HTTPException: 400 if trying to delete master account or the account name is invalid, 404 if account not found
    """
    validate_safe_name(account_name, "account name")
    try:
        if account_name == "master_account":
            raise HTTPException(status_code=400, detail="Cannot delete master account.")
        await accounts_service.delete_account(account_name)
        return {"message": "Account deleted successfully."}
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/delete-credential/{account_name}/{connector_name}")
async def delete_credential(account_name: str, connector_name: str, accounts_service: AccountsService = Depends(get_accounts_service)):
    """
    Delete a specific connector credential for an account.

    Args:
        account_name: Name of the account
        connector_name: Name of the connector to delete credentials for

    Returns:
        Success message when credential is deleted

    Raises:
        HTTPException: 404 if credential not found
    """
    try:
        await accounts_service.delete_credentials(account_name, connector_name)
        return {"message": "Credential deleted successfully."}
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/add-credential/{account_name}/{connector_name}", status_code=status.HTTP_201_CREATED)
async def add_credential(account_name: str, connector_name: str, credentials: Dict, accounts_service: AccountsService = Depends(get_accounts_service)):
    """
    Add or update connector credentials (API keys) for a specific account and connector.

    Args:
        account_name: Name of the account
        connector_name: Name of the connector
        credentials: Dictionary containing the connector credentials

    Returns:
        Success message when credentials are added

    Raises:
        HTTPException: 400 if there's an error adding the credentials
    """
    try:
        await accounts_service.add_credentials(account_name, connector_name, credentials)
        return {"message": "Connector credentials added successfully."}
    except Exception as e:
        # Rollback is handled inside add_credentials, which only deletes the file for a
        # brand-new creation and preserves pre-existing credentials on a failed update.
        raise HTTPException(status_code=400, detail=str(e))


# ============================================
# Gateway Wallet Management Endpoints
# ============================================

@router.get("/gateway/wallets")
async def list_gateway_wallets(accounts_service: AccountsService = Depends(get_accounts_service)):
    """
    List all wallets managed by Gateway.
    Gateway manages its own encrypted wallet storage.

    Returns:
        List of wallet information from Gateway

    Raises:
        HTTPException: 503 if Gateway unavailable
    """
    try:
        wallets = await accounts_service.get_gateway_wallets()
        return wallets
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/gateway/wallet-balances")
async def list_gateway_wallet_balances(
    chain: str = Query(..., min_length=1),
    network: str = Query(..., min_length=1),
    tokens: str = Query(..., min_length=1),
    accounts_service: AccountsService = Depends(get_accounts_service),
):
    """Return requested token balances for every Gateway wallet on a chain."""
    try:
        requested_tokens = list(dict.fromkeys(
            item.strip() for item in tokens.split(",") if item.strip()
        ))
        if not requested_tokens:
            raise HTTPException(status_code=400, detail="至少需要一个 token")
        wallets = await accounts_service.get_gateway_wallets()
        addresses: list[str] = []
        seen: set[str] = set()
        for wallet in wallets or []:
            if str(wallet.get("chain") or "").lower() != chain.lower():
                continue
            for address in (
                list(wallet.get("walletAddresses") or [])
                + list(wallet.get("hardwareWalletAddresses") or [])
            ):
                normalized = str(address).lower()
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    addresses.append(str(address))

        async def fetch(address: str) -> dict:
            try:
                result = await accounts_service.gateway_client.get_balances(
                    chain, network, address, tokens=requested_tokens
                )
                balances = result.get("balances", {}) if isinstance(result, dict) else {}
                normalized = {str(symbol).lower(): amount for symbol, amount in balances.items()}
                values = {
                    token: str(normalized.get(token.lower(), 0))
                    for token in requested_tokens
                }
                return {"address": address, "balances": values, "error": None}
            except Exception as exc:
                return {"address": address, "balances": None, "error": str(exc)}

        results_task = asyncio.gather(*(fetch(address) for address in addresses))
        prices_task = accounts_service.gateway_wallet_service.get_gateway_prices(
            chain, network, requested_tokens
        )
        results, prices = await asyncio.gather(results_task, prices_task)
        return {
            "chain": chain,
            "network": network,
            "tokens": requested_tokens,
            "prices": {token: str(price) for token, price in prices.items()},
            "wallets": results,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/gateway/wallet-allowances")
async def get_gateway_wallet_allowances(
    chain: str = Query(..., min_length=1),
    network: str = Query(..., min_length=1),
    address: str = Query(..., min_length=1),
    spender: str = Query(..., min_length=1),
    tokens: str = Query(..., min_length=1),
    accounts_service: AccountsService = Depends(get_accounts_service),
):
    """Return one Gateway wallet's read-only token allowances."""
    requested_tokens = list(dict.fromkeys(
        item.strip() for item in tokens.split(",") if item.strip()
    ))
    if not requested_tokens:
        raise HTTPException(status_code=400, detail="至少需要一个 token")
    try:
        result = await accounts_service.gateway_client.get_allowances(
            chain, network, address, spender, tokens=requested_tokens
        )
        if result is None:
            raise HTTPException(status_code=503, detail="Gateway 不可用")
        if isinstance(result, dict) and result.get("error"):
            raise HTTPException(
                status_code=int(result.get("status") or 502),
                detail=str(result["error"]),
            )
        # Gateway 对从未授权的代币可能省略字段；在一次有效响应中，缺失即为 0，
        # 这样前端能区分“未设置”与真正的查询失败。
        if isinstance(result, dict):
            approvals = dict(result.get("approvals") or {})
            for token in requested_tokens:
                approvals.setdefault(token, "0")
            result = {**result, "approvals": approvals}
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/gateway/wallet-approve")
async def approve_gateway_wallet_token(
    chain: str = Query(..., min_length=1),
    network: str = Query(..., min_length=1),
    address: str = Query(..., min_length=1),
    spender: str = Query(..., min_length=1),
    token: str = Query(..., min_length=1),
    amount: str = Query(..., min_length=1),
    bot_name: str | None = Query(default=None, min_length=1),
    controller_id: str | None = Query(default=None, min_length=1),
    accounts_service: AccountsService = Depends(get_accounts_service),
    bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator),
):
    """Submit one user-requested, finite Gateway token approval.

    This endpoint is deliberately separate from allowance reads.  Nothing in
    the status refresh path can create an approval transaction.
    """
    try:
        parsed_amount = Decimal(amount)
    except (InvalidOperation, ValueError):
        raise HTTPException(status_code=422, detail="授权额度必须是有效数字")
    if not parsed_amount.is_finite() or parsed_amount <= 0:
        raise HTTPException(status_code=422, detail="授权额度必须大于 0")

    try:
        result = await accounts_service.gateway_client.approve_token(
            chain=chain,
            network=network,
            address=address,
            spender=spender,
            token=token,
            amount=format(parsed_amount, "f"),
        )
        if result is None:
            raise HTTPException(status_code=503, detail="Gateway 不可用")
        if isinstance(result, dict) and result.get("error"):
            raise HTTPException(
                status_code=int(result.get("status") or 502),
                detail=str(result["error"]),
            )
        transaction_hash = str(result.get("signature") or result.get("txHash") or result.get("hash") or "").strip()
        if _is_real_transaction_hash(transaction_hash):
            approval_data = result.get("data") if isinstance(result.get("data"), dict) else {}
            # Gateway 的 approve 会等待交易确认，并在此响应中返回真实总 Gas。
            # 其后续 poll 接口的 fee 固定为空，故必须在这里保存。
            approval_confirmed = result.get("status") == 1
            await bots_manager.save_wallet_approval(
                bot_name=bot_name or WALLET_AUTHORIZATION_BOT_NAME,
                controller_id=controller_id or WALLET_AUTHORIZATION_CONTROLLER_ID,
                wallet_address=address,
                amount=format(parsed_amount, "f"),
                transaction_hash=transaction_hash,
                status="CONFIRMED" if approval_confirmed else "PENDING",
                gas_fee_native=approval_data.get("fee") if approval_confirmed else None,
            )
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"授权交易提交失败: {e}")


@router.get("/gateway/wallet-approve-preview")
async def preview_gateway_wallet_token_approval(
    chain: str = Query(..., min_length=1),
    network: str = Query(..., min_length=1),
    address: str = Query(..., min_length=1),
    spender: str = Query(..., min_length=1),
    token: str = Query(..., min_length=1),
    amount: str = Query(..., min_length=1),
    accounts_service: AccountsService = Depends(get_accounts_service),
    bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator),
):
    """Estimate the approval calls Gateway would need without signing or broadcasting anything."""
    try:
        parsed_amount = Decimal(amount)
    except (InvalidOperation, ValueError):
        raise HTTPException(status_code=422, detail="授权额度必须是有效数字")
    if not parsed_amount.is_finite() or parsed_amount <= 0:
        raise HTTPException(status_code=422, detail="授权额度必须大于 0")

    # Gateway's uniswap/router approval uses two layers: token -> Permit2, then Permit2 -> Router.
    # Its actual implementation reserves 100,000 gas units for each layer.
    permit2_address = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
    try:
        effective_response = await accounts_service.gateway_client.get_allowances(
            chain, network, address, spender, tokens=[token],
        )
        token_response = await accounts_service.gateway_client.get_allowances(
            chain, network, address, permit2_address, tokens=[token],
        )
        effective = Decimal(str((effective_response.get("approvals", {}) or {}).get(token, "0")))
        token_to_permit2 = Decimal(str((token_response.get("approvals", {}) or {}).get(token, "0")))
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"无法读取当前授权状态：{exc}")

    if effective >= parsed_amount:
        action_count = 0
        message = "当前有效授权已覆盖此额度，不会提交授权交易，也不会产生 Gas；Gateway 不会借此操作降低已有额度。"
    elif token_to_permit2 >= parsed_amount:
        action_count = 1
        message = "仅需更新 Permit2 → Uniswap Router 授权。"
    else:
        action_count = 2
        message = "需要依次完成 USDG → Permit2 与 Permit2 → Uniswap Router 两笔授权。"

    estimated_gas = Decimal("0")
    fee_per_gas = None
    if action_count:
        try:
            gas_response = await accounts_service.gateway_client.estimate_gas(chain, network)
            fee_per_gas = Decimal(str(gas_response["feePerComputeUnit"]))
            # feePerComputeUnit is gwei; Gateway assigns 100,000 gas units to every approval call.
            estimated_gas = fee_per_gas * Decimal("100000") * Decimal(action_count) / Decimal("1000000000")
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"无法读取当前 Gas 价格：{exc}")

    response = {
        "status": "success",
        "amount": float(parsed_amount),
        "action_count": action_count,
        "estimated_gas_eth": float(estimated_gas),
        "fee_per_gas_gwei": float(fee_per_gas) if fee_per_gas is not None else None,
        "message": message,
    }
    await bots_manager.save_wallet_approval_gas_estimate(
        wallet_address=address,
        token=token,
        approval_amount=parsed_amount,
        action_count=action_count,
        fee_per_gas_gwei=fee_per_gas,
        estimated_gas_eth=estimated_gas,
    )
    return response


@router.post("/gateway/add-wallet", status_code=status.HTTP_201_CREATED)
async def add_gateway_wallet(
    wallet_credential: GatewayWalletCredential,
    accounts_service: AccountsService = Depends(get_accounts_service)
):
    """
    Add an existing wallet to Gateway using its private key.
    Gateway handles encryption and storage internally.

    Args:
        wallet_credential: Wallet credentials (chain, private_key, and optional set_default)

    Returns:
        Wallet information from Gateway including address

    Raises:
        HTTPException: 503 if Gateway unavailable, 400 on validation error
    """
    try:
        result = await accounts_service.add_gateway_wallet(
            chain=wallet_credential.chain,
            private_key=wallet_credential.private_key,
            set_default=wallet_credential.set_default
        )
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/gateway/wallet/set-default")
async def set_default_gateway_wallet(
    request: SetDefaultWalletRequest,
    accounts_service: AccountsService = Depends(get_accounts_service)
) -> Dict:
    """
    Set the default wallet for a chain in Gateway.

    When multiple wallets are configured for a chain, this endpoint allows
    switching which wallet is used as the default for operations.

    Args:
        request: Contains chain and wallet address to set as default

    Returns:
        Dict with success status and updated wallet info.

    Example: POST /accounts/gateway/wallet/set-default
    {
        "chain": "solana",
        "address": "82SggYRE2Vo4jN4a2pk3aQ4SET4ctafZJGbowmCqyHx5"
    }
    """
    try:
        if not await accounts_service.gateway_client.ping():
            raise HTTPException(status_code=503, detail="Gateway service is not available")

        result = await accounts_service.gateway_client.set_default_wallet(
            chain=request.chain,
            address=request.address
        )

        if result is None:
            raise HTTPException(status_code=502, detail="Failed to set default wallet: Gateway returned no response")

        if "error" in result:
            raise HTTPException(status_code=400, detail=f"Failed to set default wallet: {result.get('error')}")

        return {
            "success": True,
            "message": f"Set {request.address} as default wallet for {request.chain}",
            "chain": request.chain,
            "address": request.address
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error setting default wallet: {str(e)}")


@router.delete("/gateway/{chain}/{address}")
async def remove_gateway_wallet(
    chain: str,
    address: str,
    accounts_service: AccountsService = Depends(get_accounts_service)
):
    """
    Remove a wallet from Gateway.

    Args:
        chain: Blockchain chain (e.g., 'solana', 'ethereum')
        address: Wallet address to remove

    Returns:
        Success message

    Raises:
        HTTPException: 503 if Gateway unavailable
    """
    try:
        result = await accounts_service.remove_gateway_wallet(chain, address)
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
