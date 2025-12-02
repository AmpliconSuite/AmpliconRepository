#!/usr/bin/env python3
"""
Memory profiling decorator to add to specific functions in views.py

This provides detailed memory profiling for functions suspected of memory leaks.
"""

import functools
import tracemalloc
import gc
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def profile_memory(func):
    """
    Decorator to profile memory usage of a function.
    
    Usage:
        @profile_memory
        def my_function():
            # ... code ...
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        # Force garbage collection before measuring
        gc.collect()
        gc.collect()
        
        # Start tracking
        tracemalloc.start()
        
        # Get current memory snapshot
        import psutil
        process = psutil.Process()
        mem_before = process.memory_info().rss / 1024 / 1024  # MB
        
        logger.info(f"[MEMORY] {func.__name__} - Starting")
        logger.info(f"[MEMORY] {func.__name__} - Memory before: {mem_before:.2f} MB")
        
        try:
            # Call the actual function
            result = func(*args, **kwargs)
            
            # Get memory after
            snapshot = tracemalloc.take_snapshot()
            mem_after = process.memory_info().rss / 1024 / 1024  # MB
            mem_diff = mem_after - mem_before
            
            # Get top memory allocations
            top_stats = snapshot.statistics('lineno')
            
            logger.info(f"[MEMORY] {func.__name__} - Memory after: {mem_after:.2f} MB")
            logger.info(f"[MEMORY] {func.__name__} - Memory diff: {mem_diff:+.2f} MB")
            
            # Log top 5 memory allocations
            logger.info(f"[MEMORY] {func.__name__} - Top 5 allocations:")
            for stat in top_stats[:5]:
                logger.info(f"  {stat}")
            
            # Force garbage collection after
            gc.collect()
            gc.collect()
            
            mem_after_gc = process.memory_info().rss / 1024 / 1024
            mem_recovered = mem_after - mem_after_gc
            
            if mem_recovered > 0:
                logger.info(f"[MEMORY] {func.__name__} - Memory recovered by GC: {mem_recovered:.2f} MB")
            
            if mem_diff > 50:  # More than 50MB not recovered
                logger.warning(f"[MEMORY] {func.__name__} - POTENTIAL LEAK: {mem_diff:.2f} MB not released")
            
            return result
            
        finally:
            tracemalloc.stop()
    
    return wrapper


def log_object_counts(label=""):
    """
    Log counts of Python objects by type.
    Useful for tracking object growth.
    """
    import gc
    from collections import Counter
    
    gc.collect()
    
    # Count objects by type
    objects = gc.get_objects()
    type_counts = Counter(type(obj).__name__ for obj in objects)
    
    logger.info(f"[OBJECTS] {label} - Total objects: {len(objects)}")
    logger.info(f"[OBJECTS] {label} - Top 10 types:")
    for obj_type, count in type_counts.most_common(10):
        logger.info(f"  {obj_type}: {count}")


def track_referrers(obj, label=""):
    """
    Track what's holding references to an object.
    Useful for debugging why objects aren't being garbage collected.
    """
    import gc
    import sys
    
    referrers = gc.get_referrers(obj)
    logger.info(f"[REFERRERS] {label} - Object has {len(referrers)} referrers")
    
    for i, ref in enumerate(referrers[:5]):  # Show first 5
        logger.info(f"  [{i}] Type: {type(ref).__name__}, Size: {sys.getsizeof(ref)} bytes")


class MemoryContext:
    """
    Context manager for tracking memory usage in a code block.
    
    Usage:
        with MemoryContext("operation_name"):
            # ... code to profile ...
    """
    def __init__(self, label=""):
        self.label = label
        self.mem_before = 0
        
    def __enter__(self):
        gc.collect()
        gc.collect()
        
        import psutil
        process = psutil.Process()
        self.mem_before = process.memory_info().rss / 1024 / 1024
        
        logger.info(f"[MEM_CTX] {self.label} - Starting: {self.mem_before:.2f} MB")
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        import psutil
        process = psutil.Process()
        mem_after = process.memory_info().rss / 1024 / 1024
        mem_diff = mem_after - self.mem_before
        
        logger.info(f"[MEM_CTX] {self.label} - Ending: {mem_after:.2f} MB")
        logger.info(f"[MEM_CTX] {self.label} - Change: {mem_diff:+.2f} MB")
        
        if mem_diff > 10:
            logger.warning(f"[MEM_CTX] {self.label} - Significant memory growth: {mem_diff:.2f} MB")
        
        gc.collect()
        gc.collect()


# Example usage in views.py:
"""
from memory_profiler import profile_memory, MemoryContext, log_object_counts

@profile_memory
def create_project(request):
    # ... existing code ...
    pass

@profile_memory
def edit_project_page(request, project_name):
    # ... existing code ...
    pass

def some_function():
    with MemoryContext("aggregator_processing"):
        agg = Aggregator(...)
        # ... use aggregator ...
    # Memory tracked and logged automatically
"""

