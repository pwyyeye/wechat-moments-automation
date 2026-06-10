# locator 子包 —— 版本无关的界面元素定位系统
from .ocr_locator import OCRLocator
from .feature_locator import FeatureLocator
from .anchor_locator import AnchorCalibrator
from .router import LocateRouter, ElementDescriptor, MOMENTS_ELEMENTS
from .template_extractor import TemplateExtractor, update_all_templates
from .resource_extractor import ResourceCollector, PEResourceExtractor, GDIImageHooker
from .wechat_native_ocr import UnifiedOCREngine, WeChatOCREngine, WeChatOCRLineResult
