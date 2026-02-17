#!/usr/bin/env python3
"""
MORNING RESEARCH REPORT
Run this at/before market open to get comprehensive catalyst analysis
"""

import os
import sys
import logging
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(message)s')

def main():
    print("\n" + "="*100)
    print("🌅 CATALYST MORNING RESEARCH REPORT".center(100))
    print(f"   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}".center(100))
    print("="*100 + "\n")
    
    try:
        from src.data.research_engine import create_research_engine
        
        # Get Finnhub key
        finnhub_key = os.getenv("FINNHUB_API_KEY")
        if not finnhub_key:
            print("⚠️  WARNING: FINNHUB_API_KEY not set - some sources will be limited")
        
        # Create research engine
        print("Initializing research engine...")
        engine = create_research_engine(finnhub_key=finnhub_key)
        
        # Run morning research
        print("\nScanning all catalyst sources...\n")
        report_data = engine.run_morning_research(output_dir="data/research_reports")
        
        # Print trading candidates
        if engine.ranked_opportunities:
            engine.print_trading_candidates(max_count=20)
        else:
            print("⚠️  No trading candidates identified at this time")
        
        # Summary
        print("\n" + "="*100)
        print("📊 SUMMARY")
        print("="*100)
        print(f"Total catalyst stocks: {report_data['total_catalysts']}")
        print(f"Ranked opportunities: {report_data['ranked']}")
        print(f"Meet trading criteria (>70 score): {len([o for o in engine.ranked_opportunities if o.combined_score > 70])}")
        
        # Print report to file
        report_path = f"data/research_reports/morning_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        os.makedirs(os.path.dirname(report_path), exist_ok=True)
        with open(report_path, 'w') as f:
            f.write(report_data['report'] if 'report' in report_data else "No report generated")
        
        print(f"\n✅ Report saved: {report_path}\n")
        
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
