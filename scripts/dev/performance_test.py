#!/usr/bin/env python3
"""
Simple performance test script for comparing Django dev server vs Gunicorn.
Tests common endpoints with concurrent requests.
"""
import time
import statistics
import requests
import concurrent.futures
from typing import List, Dict, Tuple
import argparse
import sys

def test_endpoint(url: str, timeout: int = 30) -> Tuple[float, int, bool]:
    """
    Test a single request to an endpoint.
    Returns: (response_time, status_code, success)
    """
    try:
        start = time.time()
        response = requests.get(url, timeout=timeout)
        elapsed = time.time() - start
        return (elapsed, response.status_code, True)
    except Exception as e:
        print(f"Error: {e}")
        return (timeout, 0, False)

def run_concurrent_tests(url: str, num_requests: int, num_workers: int) -> List[Tuple[float, int, bool]]:
    """
    Run concurrent requests to the endpoint.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = [executor.submit(test_endpoint, url) for _ in range(num_requests)]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]
    return results

def analyze_results(results: List[Tuple[float, int, bool]]) -> Dict:
    """
    Analyze the test results.
    """
    successful = [r[0] for r in results if r[2]]
    failed = len([r for r in results if not r[2]])
    
    if not successful:
        return {
            'total_requests': len(results),
            'successful': 0,
            'failed': failed,
            'success_rate': 0.0,
            'min_time': 0,
            'max_time': 0,
            'mean_time': 0,
            'median_time': 0,
            'p95_time': 0,
            'p99_time': 0,
        }
    
    successful_sorted = sorted(successful)
    
    return {
        'total_requests': len(results),
        'successful': len(successful),
        'failed': failed,
        'success_rate': (len(successful) / len(results)) * 100,
        'min_time': min(successful),
        'max_time': max(successful),
        'mean_time': statistics.mean(successful),
        'median_time': statistics.median(successful),
        'p95_time': successful_sorted[int(len(successful_sorted) * 0.95)] if len(successful_sorted) > 1 else successful[0],
        'p99_time': successful_sorted[int(len(successful_sorted) * 0.99)] if len(successful_sorted) > 1 else successful[0],
    }

def print_results(server_name: str, results: Dict):
    """
    Pretty print the test results.
    """
    print(f"\n{'='*60}")
    print(f"  {server_name} Performance Results")
    print(f"{'='*60}")
    print(f"Total Requests:    {results['total_requests']}")
    print(f"Successful:        {results['successful']}")
    print(f"Failed:            {results['failed']}")
    print(f"Success Rate:      {results['success_rate']:.1f}%")
    print(f"\nResponse Times (seconds):")
    print(f"  Min:             {results['min_time']:.3f}s")
    print(f"  Max:             {results['max_time']:.3f}s")
    print(f"  Mean:            {results['mean_time']:.3f}s")
    print(f"  Median:          {results['median_time']:.3f}s")
    print(f"  95th percentile: {results['p95_time']:.3f}s")
    print(f"  99th percentile: {results['p99_time']:.3f}s")
    
    # Calculate requests per second
    if results['mean_time'] > 0:
        rps = 1 / results['mean_time']
        print(f"\nThroughput:        {rps:.2f} requests/second (single thread)")
    print(f"{'='*60}\n")

def compare_results(dev_results: Dict, gunicorn_results: Dict):
    """
    Compare and show improvement from dev to gunicorn.
    """
    print(f"\n{'='*60}")
    print(f"  Performance Comparison")
    print(f"{'='*60}")
    
    if dev_results['mean_time'] > 0 and gunicorn_results['mean_time'] > 0:
        speedup = dev_results['mean_time'] / gunicorn_results['mean_time']
        print(f"Mean Response Time:")
        print(f"  Dev Server:      {dev_results['mean_time']:.3f}s")
        print(f"  Gunicorn:        {gunicorn_results['mean_time']:.3f}s")
        print(f"  Improvement:     {speedup:.2f}x {'faster' if speedup > 1 else 'slower'}")
        
        print(f"\nMedian Response Time:")
        speedup_median = dev_results['median_time'] / gunicorn_results['median_time']
        print(f"  Dev Server:      {dev_results['median_time']:.3f}s")
        print(f"  Gunicorn:        {gunicorn_results['median_time']:.3f}s")
        print(f"  Improvement:     {speedup_median:.2f}x {'faster' if speedup_median > 1 else 'slower'}")
        
        print(f"\n95th Percentile Response Time:")
        speedup_p95 = dev_results['p95_time'] / gunicorn_results['p95_time']
        print(f"  Dev Server:      {dev_results['p95_time']:.3f}s")
        print(f"  Gunicorn:        {gunicorn_results['p95_time']:.3f}s")
        print(f"  Improvement:     {speedup_p95:.2f}x {'faster' if speedup_p95 > 1 else 'slower'}")
        
        print(f"\nSuccess Rate:")
        print(f"  Dev Server:      {dev_results['success_rate']:.1f}%")
        print(f"  Gunicorn:        {gunicorn_results['success_rate']:.1f}%")
    
    print(f"{'='*60}\n")

def main():
    parser = argparse.ArgumentParser(description='Test Django server performance')
    parser.add_argument('--url', default='http://localhost:8000/', 
                       help='URL to test (default: http://localhost:8000/)')
    parser.add_argument('--requests', type=int, default=100,
                       help='Number of requests to send (default: 100)')
    parser.add_argument('--concurrency', type=int, default=10,
                       help='Number of concurrent requests (default: 10)')
    parser.add_argument('--warmup', type=int, default=5,
                       help='Number of warmup requests (default: 5)')
    
    args = parser.parse_args()
    
    print(f"\n{'='*60}")
    print(f"  Amplicon Repository Performance Test")
    print(f"{'='*60}")
    print(f"URL:               {args.url}")
    print(f"Total Requests:    {args.requests}")
    print(f"Concurrency:       {args.concurrency}")
    print(f"Warmup Requests:   {args.warmup}")
    print(f"{'='*60}\n")
    
    # Check if server is responding
    print("Checking server availability...")
    try:
        response = requests.get(args.url, timeout=10)
        print(f"✓ Server is responding (Status: {response.status_code})")
    except Exception as e:
        print(f"✗ Cannot connect to server: {e}")
        print("\nMake sure your server is running at", args.url)
        sys.exit(1)
    
    # Warmup
    print(f"\nWarming up with {args.warmup} requests...")
    for _ in range(args.warmup):
        test_endpoint(args.url)
    print("✓ Warmup complete")
    
    # Run the test
    print(f"\nRunning performance test with {args.requests} requests...")
    print(f"(Concurrency: {args.concurrency} threads)\n")
    
    start_time = time.time()
    results = run_concurrent_tests(args.url, args.requests, args.concurrency)
    total_time = time.time() - start_time
    
    # Analyze and print results
    stats = analyze_results(results)
    print_results("Test Results", stats)
    
    print(f"Total test duration: {total_time:.2f}s")
    print(f"Average throughput: {args.requests / total_time:.2f} requests/second\n")
    
    # Recommendations
    print("Recommendations:")
    if stats['mean_time'] > 1.0:
        print("  • Mean response time is high (>1s). Consider:")
        print("    - Increasing GUNICORN_WORKERS")
        print("    - Enabling GUNICORN_THREADS")
        print("    - Database query optimization")
    
    if stats['success_rate'] < 100:
        print(f"  • {stats['failed']} requests failed. Consider:")
        print("    - Increasing timeout settings")
        print("    - Checking server logs for errors")
        print("    - Reducing concurrency")
    
    if stats['p99_time'] > stats['mean_time'] * 3:
        print("  • High variance in response times. Consider:")
        print("    - Investigating slow queries")
        print("    - Checking for resource contention")

if __name__ == '__main__':
    main()

