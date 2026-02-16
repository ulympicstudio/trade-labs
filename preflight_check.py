#!/usr/bin/env python3
"""
Pre-Flight Check for Trade Labs Hybrid System
Verifies all dependencies and configurations before running.
"""

import sys
from pathlib import Path

def check_python_version():
    """Check Python version is compatible."""
    version = sys.version_info
    print(f"🐍 Python Version: {version.major}.{version.minor}.{version.micro}")
    
    if version.major < 3 or (version.major == 3 and version.minor < 9):
        print("   ❌ Python 3.9+ required")
        return False
    
    print("   ✅ Python version OK")
    return True


def check_required_packages():
    """Check all required Python packages are installed."""
    print("\n📦 Checking Python Packages:")
    
    required_packages = {
        'ib_insync': 'IB API integration',
        'pandas': 'Data manipulation',
        'numpy': 'Numerical operations',
        'feedparser': 'RSS feed parsing (news)',
        'beautifulsoup4': 'HTML parsing (news)',
        'requests': 'HTTP requests',
    }
    
    optional_packages = {
        'pytz': 'Timezone handling',
        'python-dotenv': 'Environment variables',
        'loguru': 'Enhanced logging',
    }
    
    all_ok = True
    missing_required = []
    missing_optional = []
    
    # Check required packages
    for package, description in required_packages.items():
        try:
            if package == 'beautifulsoup4':
                __import__('bs4')
                module_name = 'bs4'
            else:
                module_name = package.replace('-', '_')
                __import__(module_name)
            
            print(f"   ✅ {package:<20} - {description}")
        except ImportError:
            print(f"   ❌ {package:<20} - {description} - MISSING")
            missing_required.append(package)
            all_ok = False
    
    # Check optional packages
    print("\n   Optional packages:")
    for package, description in optional_packages.items():
        try:
            module_name = package.replace('-', '_')
            __import__(module_name)
            print(f"   ✅ {package:<20} - {description}")
        except ImportError:
            print(f"   ⚠️  {package:<20} - {description} - missing (optional)")
            missing_optional.append(package)
    
    if missing_required:
        print(f"\n   ❌ Missing required packages: {', '.join(missing_required)}")
        print(f"   Install with: pip install {' '.join(missing_required)}")
    
    if missing_optional:
        print(f"\n   💡 Optional packages available: pip install {' '.join(missing_optional)}")
    
    return all_ok


def check_ib_connection():
    """Check if can connect to IB TWS/Gateway."""
    print("\n🔌 Checking IB Connection:")
    
    try:
        from ib_insync import IB
        
        ib = IB()
        
        # Try to connect
        try:
            ib.connect('127.0.0.1', 7497, clientId=999)  # Test connection
            print("   ✅ Connected to IB TWS (port 7497)")
            ib.disconnect()
            return True
        except Exception as e:
            # Try Gateway port
            try:
                ib.connect('127.0.0.1', 4001, clientId=999)
                print("   ✅ Connected to IB Gateway (port 4001)")
                ib.disconnect()
                return True
            except:
                pass
            
            print("   ❌ Cannot connect to IB")
            print("   📝 Make sure TWS or IB Gateway is running")
            print("   📝 TWS: Enable API in Global Config -> API -> Settings")
            print("   📝 Check port: 7497 (TWS) or 4001 (Gateway)")
            return False
            
    except ImportError:
        print("   ❌ ib_insync not installed")
        return False


def check_project_files():
    """Check all required project files exist."""
    print("\n📁 Checking Project Files:")
    
    required_files = {
        'src/data/news_fetcher.py': 'News fetching',
        'src/data/news_sentiment.py': 'Sentiment analysis',
        'src/data/news_scorer.py': 'News scoring',
        'src/data/quant_news_integrator.py': 'Quant+News integration',
        'src/quant/technical_indicators.py': 'Technical indicators',
        'src/quant/quant_scorer.py': 'Quant scoring',
        'src/quant/quant_scanner.py': 'Market scanner',
        'src/quant/portfolio_risk_manager.py': 'Risk management',
        'run_hybrid_trading.py': 'Hybrid trading script',
    }
    
    all_ok = True
    
    for file_path, description in required_files.items():
        if Path(file_path).exists():
            print(f"   ✅ {file_path:<45} - {description}")
        else:
            print(f"   ❌ {file_path:<45} - {description} - MISSING")
            all_ok = False
    
    return all_ok


def check_api_keys():
    """Check for optional API keys."""
    print("\n🔑 Checking API Keys (Optional):")
    
    import os
    
    finnhub_key = os.environ.get('FINNHUB_API_KEY')
    
    if finnhub_key:
        print(f"   ✅ FINNHUB_API_KEY set ({finnhub_key[:8]}...)")
        print("      Enables: Earnings calendar, professional news")
    else:
        print("   ⚠️  FINNHUB_API_KEY not set (optional)")
        print("      System works without it, but earnings features disabled")
        print("      Get free key at: https://finnhub.io/register")
        print("      Set with: export FINNHUB_API_KEY='your_key'")
    
    return True


def test_imports():
    """Test that all core imports work."""
    print("\n🧪 Testing Core Imports:")
    
    all_ok = True
    
    # Test news system
    try:
        from src.data.news_fetcher import NewsFetcher
        from src.data.news_sentiment import NewsSentimentAnalyzer
        from src.data.news_scorer import NewsScorer
        print("   ✅ News system imports OK")
    except Exception as e:
        print(f"   ❌ News system import failed: {e}")
        all_ok = False
    
    # Test quant system
    try:
        from src.quant.technical_indicators import TechnicalIndicators
        from src.quant.quant_scorer import QuantScorer
        from src.quant.portfolio_risk_manager import PortfolioRiskManager
        print("   ✅ Quant system imports OK")
    except Exception as e:
        print(f"   ❌ Quant system import failed: {e}")
        all_ok = False
    
    # Test integration
    try:
        from src.data.quant_news_integrator import QuantNewsIntegrator
        print("   ✅ Integration system imports OK")
    except Exception as e:
        print(f"   ❌ Integration system import failed: {e}")
        all_ok = False
    
    return all_ok


def check_disk_space():
    """Check available disk space."""
    print("\n💾 Checking Disk Space:")
    
    import shutil
    
    total, used, free = shutil.disk_usage("/")
    
    free_gb = free // (2**30)
    
    print(f"   Available: {free_gb} GB")
    
    if free_gb < 1:
        print("   ⚠️  Low disk space (< 1 GB)")
    else:
        print("   ✅ Sufficient disk space")
    
    return True


def main():
    """Run all pre-flight checks."""
    print("="*80)
    print("TRADE LABS - PRE-FLIGHT CHECK")
    print("="*80)
    
    checks = [
        ("Python Version", check_python_version),
        ("Required Packages", check_required_packages),
        ("IB Connection", check_ib_connection),
        ("Project Files", check_project_files),
        ("API Keys", check_api_keys),
        ("Core Imports", test_imports),
        ("Disk Space", check_disk_space),
    ]
    
    results = {}
    
    for check_name, check_func in checks:
        try:
            results[check_name] = check_func()
        except Exception as e:
            print(f"\n   ❌ {check_name} check failed with error: {e}")
            results[check_name] = False
    
    # Summary
    print("\n" + "="*80)
    print("SUMMARY")
    print("="*80)
    
    critical_checks = ["Python Version", "Required Packages", "Project Files", "Core Imports"]
    important_checks = ["IB Connection"]
    optional_checks = ["API Keys", "Disk Space"]
    
    critical_ok = all(results.get(check, False) for check in critical_checks)
    important_ok = all(results.get(check, False) for check in important_checks)
    
    print("\nCritical (must pass):")
    for check in critical_checks:
        status = "✅" if results.get(check, False) else "❌"
        print(f"   {status} {check}")
    
    print("\nImportant (needed for trading):")
    for check in important_checks:
        status = "✅" if results.get(check, False) else "❌"
        print(f"   {status} {check}")
    
    print("\nOptional (enhances system):")
    for check in optional_checks:
        status = "✅" if results.get(check, False) else "⚠️"
        print(f"   {status} {check}")
    
    print("\n" + "="*80)
    
    if critical_ok and important_ok:
        print("✅ ALL SYSTEMS GO - Ready to run hybrid trading system")
        print("\nNext steps:")
        print("   1. Run: python run_hybrid_trading.py")
        print("   2. Review approved positions")
        print("   3. Execute trades via execution pipeline")
        return 0
    elif critical_ok:
        print("⚠️  SYSTEM READY (with warnings)")
        print("\n⚠️  IB Connection failed - start TWS/Gateway before trading")
        print("\nFor testing without IB:")
        print("   python test_news_integration.py  # News system only")
        return 1
    else:
        print("❌ SYSTEM NOT READY - Fix critical issues above")
        print("\nTo fix:")
        print("   1. Install missing packages")
        print("   2. Verify project files are present")
        print("   3. Run this check again")
        return 2


if __name__ == "__main__":
    sys.exit(main())
