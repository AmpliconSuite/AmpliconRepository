"""
Memory leak tracking utilities for debugging edit_project_page and background threads.
"""
import tracemalloc
import gc
import sys
import logging
import weakref
from collections import defaultdict
from datetime import datetime
import threading

logger = logging.getLogger(__name__)


class MemorySnapshot:
    """Captures a snapshot of memory state for comparison."""
    
    def __init__(self, name, take_snapshot=True):
        self.name = name
        self.timestamp = datetime.now()
        self.snapshot = None
        self.gc_stats = None
        self.tracked_objects = {}
        
        if take_snapshot and tracemalloc.is_tracing():
            self.snapshot = tracemalloc.take_snapshot()
            self.gc_stats = {
                'collected': gc.collect(),
                'garbage': len(gc.garbage),
                'counts': gc.get_count()
            }
    
    def compare_to(self, other_snapshot, top_n=20):
        """Compare this snapshot to another and log differences."""
        if not self.snapshot or not other_snapshot.snapshot:
            logger.warning(f"Cannot compare snapshots - one is missing")
            return
        
        stats = self.snapshot.compare_to(other_snapshot.snapshot, 'lineno')
        
        logger.error(f"\n{'='*80}")
        logger.error(f"Memory Growth: {other_snapshot.name} → {self.name}")
        logger.error(f"Time elapsed: {(self.timestamp - other_snapshot.timestamp).total_seconds():.2f}s")
        logger.error(f"{'='*80}")
        
        total_growth = sum(stat.size_diff for stat in stats if stat.size_diff > 0)
        logger.error(f"Total memory growth: {total_growth / 1024 / 1024:.2f} MB")
        
        logger.error(f"\nTop {top_n} memory increases:")
        for i, stat in enumerate(stats[:top_n], 1):
            if stat.size_diff > 0:
                logger.error(f"{i}. {stat}")
        
        # GC comparison
        if self.gc_stats and other_snapshot.gc_stats:
            logger.error(f"\nGarbage collection stats:")
            logger.error(f"  Collected objects: {self.gc_stats['collected']}")
            logger.error(f"  Uncollectable garbage: {self.gc_stats['garbage']}")
            logger.error(f"  GC counts: {self.gc_stats['counts']}")


class ObjectTracker:
    """Tracks specific objects to detect memory leaks."""
    
    def __init__(self):
        self.tracked = {}
        self.weak_refs = {}
        self.lock = threading.Lock()
    
    def track(self, obj, name, metadata=None):
        """Track an object with a name and optional metadata."""
        with self.lock:
            obj_id = id(obj)
            self.tracked[obj_id] = {
                'name': name,
                'type': type(obj).__name__,
                'size': sys.getsizeof(obj),
                'metadata': metadata or {},
                'timestamp': datetime.now(),
                'thread_id': threading.current_thread().ident
            }
            
            # Try to create a weak reference
            try:
                self.weak_refs[obj_id] = weakref.ref(obj, lambda ref: self._on_delete(obj_id))
            except TypeError:
                # Object doesn't support weak references
                pass
            
            logger.info(f"Tracking object: {name} (id={obj_id}, type={type(obj).__name__}, size={sys.getsizeof(obj)} bytes)")
    
    def _on_delete(self, obj_id):
        """Called when a tracked object is garbage collected."""
        if obj_id in self.tracked:
            info = self.tracked[obj_id]
            logger.info(f"Object collected: {info['name']} (id={obj_id})")
            del self.tracked[obj_id]
    
    def check_leaks(self):
        """Check which tracked objects are still alive."""
        with self.lock:
            leaked = []
            for obj_id, info in list(self.tracked.items()):
                # Check if weak ref still exists
                if obj_id in self.weak_refs:
                    obj = self.weak_refs[obj_id]()
                    if obj is not None:
                        leaked.append(info)
                    else:
                        # Object was collected but callback not fired yet
                        del self.tracked[obj_id]
                else:
                    # No weak ref (object doesn't support it), assume it leaked
                    leaked.append(info)
            
            if leaked:
                logger.error(f"\n{'='*80}")
                logger.error(f"POTENTIAL MEMORY LEAKS DETECTED: {len(leaked)} objects still alive")
                logger.error(f"{'='*80}")
                
                for info in leaked:
                    age = (datetime.now() - info['timestamp']).total_seconds()
                    logger.error(f"  • {info['name']} ({info['type']}) - {info['size']} bytes - age: {age:.1f}s")
                    if info['metadata']:
                        logger.error(f"    Metadata: {info['metadata']}")
                    logger.error(f"    Thread: {info['thread_id']}")
            else:
                logger.info("No memory leaks detected - all tracked objects collected")
            
            return leaked
    
    def clear(self):
        """Clear all tracked objects."""
        with self.lock:
            self.tracked.clear()
            self.weak_refs.clear()


class ThreadMemoryTracker:
    """Tracks memory usage within background threads."""
    
    def __init__(self, thread_name):
        self.thread_name = thread_name
        self.start_snapshot = None
        self.checkpoints = []
    
    def start(self):
        """Start tracking at the beginning of a thread."""
        if not tracemalloc.is_tracing():
            tracemalloc.start()
        
        self.start_snapshot = MemorySnapshot(f"{self.thread_name}_start")
        logger.info(f"[{self.thread_name}] Memory tracking started")
    
    def checkpoint(self, name):
        """Create a checkpoint to measure memory at a specific point."""
        snapshot = MemorySnapshot(f"{self.thread_name}_{name}")
        self.checkpoints.append(snapshot)
        
        # Compare to previous checkpoint or start
        previous = self.checkpoints[-2] if len(self.checkpoints) > 1 else self.start_snapshot
        if previous:
            snapshot.compare_to(previous, top_n=10)
        
        logger.info(f"[{self.thread_name}] Checkpoint: {name}")
    
    def end(self):
        """End tracking and report total memory changes."""
        end_snapshot = MemorySnapshot(f"{self.thread_name}_end")
        
        if self.start_snapshot:
            end_snapshot.compare_to(self.start_snapshot, top_n=20)
        
        logger.info(f"[{self.thread_name}] Memory tracking completed")
        
        # Force garbage collection
        collected = gc.collect()
        logger.info(f"[{self.thread_name}] GC collected {collected} objects")


def analyze_large_objects(min_size_mb=1, top_n=20):
    """Analyze and log the largest objects in memory."""
    gc.collect()
    
    objects_by_type = defaultdict(list)
    
    for obj in gc.get_objects():
        try:
            size = sys.getsizeof(obj)
            if size > min_size_mb * 1024 * 1024:
                obj_type = type(obj).__name__
                objects_by_type[obj_type].append((obj, size))
        except:
            pass
    
    logger.error(f"\n{'='*80}")
    logger.error(f"LARGE OBJECTS ANALYSIS (>{min_size_mb}MB)")
    logger.error(f"{'='*80}")
    
    for obj_type, objects in sorted(objects_by_type.items(), key=lambda x: sum(s for _, s in x[1]), reverse=True)[:top_n]:
        total_size = sum(size for _, size in objects)
        count = len(objects)
        logger.error(f"{obj_type}: {count} objects, {total_size / 1024 / 1024:.2f} MB total")
        
        # Show details of top 3 largest
        for obj, size in sorted(objects, key=lambda x: x[1], reverse=True)[:3]:
            logger.error(f"  └─ {size / 1024 / 1024:.2f} MB - {repr(obj)[:100]}")


def track_file_objects():
    """Track all open file objects."""
    gc.collect()
    
    open_files = []
    for obj in gc.get_objects():
        try:
            if hasattr(obj, 'read') and hasattr(obj, 'close'):
                # Likely a file object
                file_info = {
                    'type': type(obj).__name__,
                    'closed': getattr(obj, 'closed', 'unknown'),
                    'name': getattr(obj, 'name', 'unknown'),
                    'mode': getattr(obj, 'mode', 'unknown')
                }
                if not file_info['closed']:
                    open_files.append(file_info)
        except:
            pass
    
    if open_files:
        logger.error(f"\n{'='*80}")
        logger.error(f"OPEN FILE HANDLES: {len(open_files)}")
        logger.error(f"{'='*80}")
        for finfo in open_files:
            logger.error(f"  • {finfo['type']}: {finfo['name']} (mode: {finfo['mode']})")
    else:
        logger.info("No open file handles detected")
    
    return open_files


def track_project_dict_references(project_name_or_id):
    """Track all references to project dictionaries."""
    gc.collect()
    
    project_refs = []
    for obj in gc.get_objects():
        try:
            if isinstance(obj, dict) and 'project_name' in obj:
                # Check if this is a project dict we care about
                if obj.get('_id') == project_name_or_id or obj.get('project_name') == project_name_or_id:
                    referrers = gc.get_referrers(obj)
                    project_refs.append({
                        'project': obj.get('project_name', 'unknown'),
                        'id': obj.get('_id', 'unknown'),
                        'size': sys.getsizeof(obj),
                        'referrer_count': len(referrers),
                        'referrer_types': [type(r).__name__ for r in referrers[:5]]
                    })
        except:
            pass
    
    if project_refs:
        logger.error(f"\n{'='*80}")
        logger.error(f"PROJECT DICTIONARY REFERENCES")
        logger.error(f"{'='*80}")
        for ref in project_refs:
            logger.error(f"Project: {ref['project']}")
            logger.error(f"  Size: {ref['size'] / 1024:.2f} KB")
            logger.error(f"  Referrers: {ref['referrer_count']}")
            logger.error(f"  Referrer types: {ref['referrer_types']}")
    
    return project_refs


def use_objgraph_analysis(obj_type='dict', max_refs=10):
    """
    Use objgraph to analyze object references and find reference chains.
    Requires: pip install objgraph
    """
    try:
        import objgraph
        
        logger.error(f"\n{'='*80}")
        logger.error(f"OBJGRAPH ANALYSIS: {obj_type}")
        logger.error(f"{'='*80}")
        
        # Show most common types
        logger.error("\nMost common object types:")
        objgraph.show_most_common_types(limit=20)
        
        # Growth tracking
        logger.error(f"\nGrowth of {obj_type} objects:")
        objgraph.show_growth(limit=20)
        
        # Find back-references for specific types
        objs = objgraph.by_type(obj_type)
        if objs:
            logger.error(f"\nFound {len(objs)} {obj_type} objects")
            
            # Analyze the largest one
            largest = max(objs, key=lambda x: sys.getsizeof(x) if hasattr(x, '__sizeof__') else 0)
            logger.error(f"\nBackrefs for largest {obj_type} (size: {sys.getsizeof(largest)} bytes):")
            objgraph.show_backrefs([largest], max_depth=5, filename=f'/tmp/objgraph_{obj_type}_refs.png')
            logger.error(f"Reference graph saved to /tmp/objgraph_{obj_type}_refs.png")
        
    except ImportError:
        logger.warning("objgraph not installed. Install with: pip install objgraph")
    except Exception as e:
        logger.error(f"Error in objgraph analysis: {e}")


# Global tracker instance
_global_tracker = ObjectTracker()

def get_global_tracker():
    """Get the global object tracker instance."""
    return _global_tracker

