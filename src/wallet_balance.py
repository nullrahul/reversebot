import asyncio
import os
import sys
from pathlib import Path

# Add project root to path so we can import config and src
sys.path.insert(0, str(Path(__file__).parent.parent))

from config.config import load_config
from src.auth.l1_auth import L1Auth
from src.auth.l2_auth import L2Auth
from src.api.clob_client import ClobClient

async def check_proxy_balance():
    """
    Standalone utility to check the real USDC balance and allowance
    on the Polymarket CTF Exchange (Proxy Wallet).
    """
    print("\n" + "="*50)
    print(" POLYMARKET PROXY WALLET BALANCE CHECK")
    print("="*50 + "\n")

    try:
        # Load configuration from .env with fallback
        env_file = ".env"
        if len(sys.argv) > 1:
            env_file = sys.argv[1]
        elif Path(".env.rd").exists():
            env_file = ".env.rd"
        config = load_config(env_file)
        
        # Initialize L1 Auth to get the address
        l1 = L1Auth(config.private_key)
        
        # Initialize L2 Auth for API signing using the EOA signer address
        l2 = L2Auth(
            api_key=config.poly_api_key,
            api_secret=config.poly_api_secret,
            passphrase=config.poly_passphrase,
            address=l1.address,
            clob_api_url=config.clob_api_url
        )
        
        # Initialize Client passing the proxy address as funder (force paper_trade=False to hit real API balance)
        client = ClobClient(
            l2, 
            clob_api_url=config.clob_api_url, 
            paper_trade=False, 
            l1_auth=l1,
            funder=config.poly_address
        )
        
        print(f"  Signer Address (EOA):   {l1.address}")
        print(f"  Funder Address (Proxy): {config.poly_address}")
        print("  Querying CLOB API for USDC (Asset ID: 215)...")
        
        # Sync server time first to avoid auth errors
        await l2.sync_server_time()
        
        balance_data = await client.get_account_balance()
        
        if balance_data:
            balance = balance_data.get('balance', '0.00')
            allowance = balance_data.get('allowance', '0.00')
            
            print(f"\n  [✔] USDC Balance:    ${float(balance):.2f}")
            print(f"  [✔] USDC Allowance:  ${float(allowance):.2f}")
            
            if float(balance) < 1.0:
                print("\n  [!] WARNING: Your balance is below $1.00. Deposits required for live trading.")
            
            if float(allowance) < 1.0:
                print("  [!] WARNING: USDC allowance is low. You must 'Approve' USDC on Polymarket.")
        else:
            print("\n  [✘] FAILED: Could not retrieve balance. Verify your API credentials in .env.")

        await client.close()

    except Exception as e:
        print(f"\n  [✘] CRITICAL ERROR: {e}")
    
    print("\n" + "="*50 + "\n")

if __name__ == "__main__":
    asyncio.run(check_proxy_balance())
