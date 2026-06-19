#!/usr/bin/env python3
"""
Diagnostic script for Telegram connection issues.
Run this to test different connection methods and identify the problem.
"""

import os
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

import config
from telethon.sync import TelegramClient
from telethon.sessions import StringSession
from telethon import connection as tl_connection


def test_direct_connection():
    """Test connection without proxy."""
    print("=" * 60)
    print("Testing DIRECT connection (no proxy)")
    print("=" * 60)
    
    try:
        with TelegramClient(
            StringSession(),
            config.TELEGRAM_API_ID,
            config.TELEGRAM_API_HASH,
            timeout=30,
            connection_retries=3,
        ) as client:
            print("✅ Direct connection successful!")
            print(f"   Connected to: {client.session.server_address}")
            return True
    except Exception as e:
        print(f"❌ Direct connection failed: {e}")
        return False


def test_with_proxy():
    """Test connection with proxy from environment."""
    print("\n" + "=" * 60)
    print("Testing PROXY connection")
    print("=" * 60)
    
    # Check for proxy / VPN settings
    proxy_env = (
        os.environ.get("TELEGRAM_VPN")
        or os.environ.get("TELEGRAM_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("HTTP_PROXY")
    )
    
    if not proxy_env:
        print("⚠️  No proxy configured in environment")
        return None
    
    proxy_env = proxy_env.strip()
    print(f"📡 Proxy: {proxy_env}")
    
    # Parse proxy (simplified version)
    from urllib.parse import urlparse
    parsed = urlparse(proxy_env)
    scheme = (parsed.scheme or "").lower()
    
    connection_cls = None
    proxy = None
    
    # VLESS VPN — start a local Xray bridge that exposes a SOCKS5 endpoint.
    if scheme == "vless":
        print("   Type: VLESS VPN (via local Xray → SOCKS5 bridge)")
        try:
            from parser.vless_bridge import start_vless_bridge
            local_host, local_port = start_vless_bridge(proxy_env)
            print(f"   Local SOCKS5: {local_host}:{local_port}")
        except Exception as e:
            print(f"❌ Failed to start VLESS bridge: {e}")
            return False
        try:
            import socks
            proxy = (socks.SOCKS5, local_host, local_port, True, None, None)
        except ImportError:
            print("❌ PySocks not installed (pip install pysocks)")
            return False
    elif scheme == "mtproxy":
        # MTProxy format: mtproxy://host:port:secret
        secret = parsed.path.lstrip("/") or parsed.password
        if secret:
            connection_cls = tl_connection.ConnectionTcpMTProxyRandomizedIntermediate
            proxy = (parsed.hostname, parsed.port, secret)
            print(f"   Type: MTProxy")
            print(f"   Host: {parsed.hostname}:{parsed.port}")
    elif scheme in ("socks5", "socks4", "http"):
        print(f"   Type: {scheme.upper()}")
        print(f"   Host: {parsed.hostname}:{parsed.port}")
        try:
            import socks
            proxy_types = {
                "socks5": socks.SOCKS5,
                "socks4": socks.SOCKS4,
                "http": socks.HTTP,
            }
            proxy = (
                proxy_types[scheme],
                parsed.hostname,
                parsed.port,
                True,
                parsed.username or None,
                parsed.password or None,
            )
        except ImportError:
            print("❌ PySocks not installed for SOCKS proxy")
            return False
    
    if connection_cls or proxy:
        try:
            client_kwargs = {
                "timeout": 30,
                "connection_retries": 3,
            }
            if connection_cls:
                client_kwargs["connection"] = connection_cls
            if proxy:
                client_kwargs["proxy"] = proxy
            
            with TelegramClient(
                StringSession(),
                config.TELEGRAM_API_ID,
                config.TELEGRAM_API_HASH,
                **client_kwargs,
            ) as client:
                print("✅ Proxy connection successful!")
                print(f"   Connected to: {client.session.server_address}")
                return True
        except ValueError as e:
            if "readexactly size can not be less than zero" in str(e):
                print("❌ MTProxy connection error (known issue)")
                print("   This proxy appears to be incompatible or misconfigured")
                return False
            raise
        except Exception as e:
            print(f"❌ Proxy connection failed: {e}")
            return False
    
    return None


def check_telethon_version():
    """Check Telethon version."""
    print("=" * 60)
    print("Telethon Version Check")
    print("=" * 60)
    
    try:
        import telethon
        print(f"✅ Telethon version: {telethon.__version__}")
        
        # Check for known issues
        version_parts = telethon.__version__.split('.')
        major, minor = int(version_parts[0]), int(version_parts[1])
        
        if major == 1 and minor >= 40:
            print("⚠️  Version 1.40+ has known MTProxy issues")
            print("   Consider using direct connection or different proxy")
        
        return True
    except Exception as e:
        print(f"❌ Could not check Telethon version: {e}")
        return False


def check_network_connectivity():
    """Check basic network connectivity."""
    print("\n" + "=" * 60)
    print("Network Connectivity Check")
    print("=" * 60)
    
    import socket
    
    # Test DNS resolution
    try:
        socket.gethostbyname("api.telegram.org")
        print("✅ DNS resolution working")
    except socket.gaierror:
        print("❌ DNS resolution failed")
        return False
    
    # Test TCP connection to Telegram servers
    telegram_servers = [
        ("149.154.167.51", 443),  # Telegram server
        ("149.154.167.50", 443),
    ]
    
    for host, port in telegram_servers:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)
            result = sock.connect_ex((host, port))
            sock.close()
            
            if result == 0:
                print(f"✅ Can connect to {host}:{port}")
                return True
            else:
                print(f"❌ Cannot connect to {host}:{port} (error code: {result})")
        except Exception as e:
            print(f"❌ Connection test failed for {host}:{port}: {e}")
    
    return False


def main():
    print("🔍 Telegram Connection Diagnostic Tool")
    print()
    
    # Check configuration
    print("=" * 60)
    print("Configuration Check")
    print("=" * 60)
    print(f"API ID: {config.TELEGRAM_API_ID}")
    print(f"API Hash: {'*' * len(config.TELEGRAM_API_HASH) if config.TELEGRAM_API_HASH else 'NOT SET'}")
    
    if not config.TELEGRAM_API_ID or not config.TELEGRAM_API_HASH:
        print("❌ API credentials not configured!")
        return
    
    # Check version
    check_telethon_version()
    
    # Check network
    network_ok = check_network_connectivity()
    
    if not network_ok:
        print("\n❌ Network connectivity issues detected")
        print("   Check your internet connection and firewall settings")
        return
    
    # Test connections
    direct_ok = test_direct_connection()
    proxy_ok = test_with_proxy()
    
    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    
    if direct_ok:
        print("✅ Direct connection works - no proxy needed")
        print("   Recommendation: Disable proxy in .env and use direct connection")
    elif proxy_ok:
        print("✅ Proxy connection works")
        print("   Recommendation: Keep proxy settings")
    else:
        print("❌ Both connection methods failed")
        print("\nPossible solutions:")
        print("1. Check if Telegram is blocked in your region")
        print("2. Try a different proxy server")
        print("3. Check your firewall/antivirus settings")
        print("4. Try connecting from a different network")
        print("5. Update Telethon: pip install --upgrade telethon")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n⏹  Interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
