# core 子包 —— 事件驱动核心
from .events import EventBus, Event, EventType, global_event_bus
from .watchers import (
    WatchManager, OCRTextWatcher, UIATreeWatcher,
    WindowWatcher, TimerWatcher
)
from .publisher import EventDrivenPublisher, PublishTask, PublishResult
