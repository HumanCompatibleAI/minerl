import psutil


def child_count() -> int:
    current_process = psutil.Process()
    children = current_process.children()
    return len(children)
