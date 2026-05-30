import asyncio
import os
import sys
from pathlib import Path
from web3 import Web3

# Add project root to path so we can import config
sys.path.insert(0, str(Path(__file__).parent.parent))

from config.config import load_config
from src.auth.l1_auth import L1Auth

async def check_onchain_balances():
    # Load configuration from .env with fallback
    env_file = ".env"
    if len(sys.argv) > 1:
        env_file = sys.argv[1]
    elif Path(".env.rd").exists():
        env_file = ".env.rd"
    
    print(f"Loading configuration from: {env_file}")
    try:
        config = load_config(env_file)
    except Exception as e:
        print(f"Error loading configuration: {e}")
        return
    
    # Initialize L1 Auth to get the address
    try:
        l1 = L1Auth(config.private_key)
        address = l1.address
    except Exception as e:
        print(f"Error initializing L1 auth: {e}")
        return

    # Try list of public RPCs
    rpcs = [
        "https://1rpc.io/matic",
        "https://polygon.drpc.org",
        "https://polygon.publicnode.com",
        "https://polygon-mainnet.public.blastapi.io"
    ]
    
    w3 = None
    connected_rpc = None
    for rpc in rpcs:
        try:
            # Set a standard User-Agent header to avoid 401 Unauthorized errors
            provider = Web3.HTTPProvider(
                rpc, 
                request_kwargs={
                    "headers": {
                        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                    }
                }
            )
            w3_temp = Web3(provider)
            if w3_temp.is_connected():
                w3 = w3_temp
                connected_rpc = rpc
                break
        except Exception as e:
            # Print fallback attempt failure at debug level
            pass
            
    if not w3:
        print("\n  [x] CRITICAL: All Polygon RPC connections failed! Please check your internet connection or RPC list.")
        return

    print("\n" + "="*50)
    print(" POLYGON ON-CHAIN WALLET BALANCE CHECK")
    print("="*50 + "\n")
    print(f"  Connected RPC:       {connected_rpc}")
    print(f"  Wallet Address:      {address}\n")
    
    # 1. Check MATIC / POL
    try:
        matic_bal = w3.eth.get_balance(address)
        matic_friendly = w3.from_wei(matic_bal, "ether")
        print(f"  [✔] MATIC Balance:    {matic_friendly:.6f} MATIC")
    except Exception as e:
        print(f"  [x] MATIC Fetch Failed: {e}")
        
    # Minimal ERC20 ABI
    abi = [{
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function"
    }]
    
    # 2. Check Bridged USDC.e (Polymarket CTF USDC)
    try:
        usdce_address = w3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
        usdce_contract = w3.eth.contract(address=usdce_address, abi=abi)
        usdce_bal = usdce_contract.functions.balanceOf(address).call()
        print(f"  [✔] Bridged USDC.e:   ${usdce_bal / 10**6:.2f}")
    except Exception as e:
        print(f"  [x] USDC.e Fetch Failed: {e}")
        
    # 3. Check Native USDC
    try:
        usdc_address = w3.to_checksum_address("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359")
        usdc_contract = w3.eth.contract(address=usdc_address, abi=abi)
        usdc_bal = usdc_contract.functions.balanceOf(address).call()
        print(f"  [✔] Native USDC:      ${usdc_bal / 10**6:.2f}")
    except Exception as e:
        print(f"  [x] USDC Fetch Failed: {e}")

    # 4. Check Polymarket USD (pUSD)
    try:
        pusd_address = w3.to_checksum_address("0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb")
        pusd_contract = w3.eth.contract(address=pusd_address, abi=abi)
        pusd_bal = pusd_contract.functions.balanceOf(address).call()
        print(f"  [✔] Polymarket pUSD:  ${pusd_bal / 10**6:.2f}")
    except Exception as e:
        print(f"  [x] pUSD Fetch Failed: {e}")
        
    print("\n" + "="*50 + "\n")

if __name__ == "__main__":
    asyncio.run(check_onchain_balances())
