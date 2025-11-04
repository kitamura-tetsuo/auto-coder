#!/usr/bin/env python3
"""
Test Watcher 個別テスト
"""

import subprocess
import sys
import time

def test_only_test_watcher():
    """Test Watcherサーバーだけをテスト"""
    print("=== Test Watcher サーバーテスト ===")
    
    try:
        # Test Watcherサーバーのみを実行
        process = subprocess.Popen(
            [sys.executable, "src/auto_coder/mcp_servers/test_watcher/server.py"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        
        print(f"✓ プロセス開始 (PID: {process.pid})")
        
        # 5秒だけ待機して出力を見る
        try:
            time.sleep(5)
            process.terminate()
            stdout, stderr = process.communicate(timeout=2)
            print("✓ プロセス終了")
            
            if stdout:
                print("=== STDOUT ===")
                print(stdout)
            
            if stderr:
                print("=== STDERR ===")
                print(stderr)
                
            return True
            
        except subprocess.TimeoutExpired:
            process.kill()
            print("✗ タイムアウト (サーバーがハングアップ)")
            return False
            
    except Exception as e:
        print(f"✗ エラー: {e}")
        return False

if __name__ == "__main__":
    test_only_test_watcher()