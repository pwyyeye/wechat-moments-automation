#!/usr/bin/env python3
"""
PC 微信朋友圈自动化 —— 主入口（事件驱动版）

使用方式：
    # 单次发布
    python main.py --text "今天天气真好" --images photo1.jpg photo2.jpg

    # 从文件批量发布
    python main.py --batch posts.txt

    # 交互模式
    python main.py --interactive

    # 定时调度模式
    python main.py --schedule

    # 查看状态
    python main.py --status

    # 空跑测试
    python main.py --text "测试" --dry-run

配置文件：
    config/settings.yaml

Author: 版本无关微信自动化系统
"""

import sys
import argparse
import signal
import json
import time
import win32gui
from pathlib import Path
from datetime import datetime

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent))

from src.core import EventDrivenPublisher, PublishTask, PublishResult
from src.core.account_manager import AccountManager, WeChatWindowFinder

# ═══════════════════════════════════════════════════════════════
# 任务持久化
# ═══════════════════════════════════════════════════════════════

STATE_FILE = Path(__file__).parent / "state.json"


def configure_console_encoding():
    """Keep Chinese text and emoji printable in the Windows console."""
    if sys.platform != 'win32':
        return
    for stream_name in ('stdout', 'stderr'):
        stream = getattr(sys, stream_name, None)
        if stream and hasattr(stream, 'reconfigure'):
            try:
                stream.reconfigure(encoding='utf-8', errors='replace')
            except (AttributeError, ValueError):
                pass


def save_state(publisher, results: list = None):
    """保存当前状态到磁盘"""
    state = {
        'saved_at': datetime.now().isoformat(),
        'daily_post_count': publisher._daily_post_count if hasattr(publisher, '_daily_post_count') else 0,
        'last_results': [
            {
                'success': r.success,
                'text': r.task.text[:50] if hasattr(r, 'task') else '',
                'elapsed': r.elapsed_seconds if hasattr(r, 'elapsed_seconds') else 0,
                'error': r.error_message if hasattr(r, 'error_message') else '',
            }
            for r in (results or [])[-10:]  # 保留最近 10 条
        ],
    }
    try:
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception:
        pass


def load_state() -> dict:
    """从磁盘恢复状态"""
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text(encoding='utf-8'))
    except Exception:
        pass
    return {}


# ═══════════════════════════════════════════════════════════════
# 优雅退出
# ═══════════════════════════════════════════════════════════════

_shutdown_requested = False
_publisher_ref = None
_agent_ref = None


def _on_signal(signum, frame):
    global _shutdown_requested
    if _shutdown_requested:
        print("\n⚠️ 强制退出（不保存状态）")
        sys.exit(1)

    _shutdown_requested = True
    print(f"\n🛑 收到退出信号，正在优雅关闭...")

    if _publisher_ref:
        try:
            results = getattr(_publisher_ref, '_stats', [])
            save_state(_publisher_ref, results)
            _publisher_ref.shutdown()
        except Exception:
            pass

    if _agent_ref:
        try:
            _agent_ref.stop()
        except Exception:
            pass

    print("👋 已安全退出")
    sys.exit(0)


signal.signal(signal.SIGINT, _on_signal)
signal.signal(signal.SIGTERM, _on_signal)

# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════

def parse_args():
    parser = argparse.ArgumentParser(
        description="PC 微信朋友圈自动化 — 版本无关 + 事件驱动",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python main.py --text "今天天气真好"
  python main.py --text "分享照片" --images photo1.jpg photo2.jpg
  python main.py --batch posts.txt
  python main.py --interactive
  python main.py --schedule
  python main.py --status
        """,
    )
    parser.add_argument('--text', type=str, help='朋友圈文字内容')
    parser.add_argument('--images', type=str, nargs='*', help='图片文件路径')
    parser.add_argument('--batch', type=str, help='批量发布文件（每行: 文字|图片1 图片2）')
    parser.add_argument('--config', type=str, help='配置文件路径')
    parser.add_argument('--interactive', action='store_true', help='交互模式')
    parser.add_argument('--schedule', action='store_true', help='定时调度模式（后台运行）')
    parser.add_argument('--status', action='store_true', help='查看系统状态')
    parser.add_argument('--calibrate', action='store_true', help='手动触发界面校准')
    parser.add_argument('--extract-templates', action='store_true', help='提取/更新图标模板库')
    parser.add_argument('--test', action='store_true', help='运行自检（不操作微信）')
    parser.add_argument('--account', type=str, help='指定微信账号（多开时使用）')
    parser.add_argument('--accounts', action='store_true', help='列出所有检测到的微信窗口')
    parser.add_argument('--dry-run', action='store_true', help='空跑模式')
    parser.add_argument(
        '--confirm-publish',
        action='store_true',
        help='显式允许点击“发表”；缺省会安全停在编辑页',
    )
    parser.add_argument('--resume', action='store_true', help='从上次中断恢复')
    parser.add_argument('--agent', action='store_true', help='启动多数据源 Windows Agent')
    parser.add_argument('--agent-config', type=str, help='Agent config.yaml 路径')
    parser.add_argument(
        '--agent-no-browser',
        action='store_true',
        help='启动 Agent 时不自动打开本地管理页',
    )
    return parser.parse_args()


def batch_from_file(filepath: str, confirm_publish: bool = False) -> list:
    """从文件读取批量发布任务"""
    tasks = []
    with open(filepath, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split('|')
            text = parts[0].strip()
            images = parts[1].strip().split() if len(parts) > 1 else []
            tasks.append(PublishTask(
                text=text,
                images=images,
                confirm_publish=confirm_publish,
            ))
    return tasks


def interactive_mode(publisher: EventDrivenPublisher):
    """交互模式"""
    print("\n" + "=" * 50)
    print("  微信朋友圈自动化 — 交互模式（事件驱动）")
    print("  输入文字后按 Enter 发布，输入 'quit' 退出")
    print("=" * 50 + "\n")

    while not _shutdown_requested:
        try:
            text = input("✏️  朋友圈内容: ").strip()

            if text.lower() in ('quit', 'exit', 'q'):
                break
            if not text:
                print("内容不能为空\n")
                continue

            img_input = input("🖼️  图片路径（可选，空格分隔，直接回车跳过）: ").strip()
            images = img_input.split() if img_input else []

            confirmation = input(
                "输入 PUBLISH 才会点击发表；直接回车仅准备到编辑页: "
            ).strip()
            task = PublishTask(
                text=text,
                images=images,
                confirm_publish=confirmation == 'PUBLISH',
            )
            result = publisher.publish(task)

            if result.published:
                print(f"✅ 发布成功！(耗时 {result.elapsed_seconds:.0f}s)\n")
            elif result.stopped_before_publish:
                print("🛑 内容已准备，安全停在发表前；请在微信中检查或手动取消\n")
            else:
                print(f"❌ 发布失败: {result.error_message}\n")

        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"❌ 错误: {e}")


def schedule_mode(publisher: EventDrivenPublisher):
    """定时调度模式 — 运行定时任务调度器"""
    print("🕐 定时调度模式启动")

    try:
        from croniter import croniter
    except ImportError:
        print("❌ 需要安装 croniter: pip install croniter")
        return

    # 从 API Server 的 schedules 中获取任务列表
    import requests

    API_BASE = "http://127.0.0.1:18080"

    print("定时调度器运行中（按 Ctrl+C 退出）...")

    # 记录上次检查的分钟，避免每分钟重复检查
    last_check_minute = -1

    while not _shutdown_requested:
        now = datetime.now()

        # 每分钟检查一次
        if now.minute == last_check_minute:
            time.sleep(1)
            continue
        last_check_minute = now.minute

        try:
            resp = requests.get(f"{API_BASE}/api/schedule", timeout=5)
            schedules = resp.json() if resp.ok else []
        except Exception:
            time.sleep(5)
            continue

        for item in schedules:
            if not item.get('enabled', True):
                continue

            cron_expr = item.get('cron', '')
            if not cron_expr:
                continue

            try:
                iter_obj = croniter(cron_expr, now)
                prev = iter_obj.get_prev(datetime)
                diff = (now - prev).total_seconds()

                # 如果上次执行时间在 2 分钟内，触发发布
                if 0 < diff < 120:
                    print(f"⏰ 触发定时任务: {item['text'][:40]}...")
                    task = PublishTask(
                        text=item.get('text', ''),
                        images=item.get('images', []),
                        confirm_publish=item.get('confirm_publish', False),
                    )
                    publisher.publish(task)
            except Exception as e:
                print(f"⚠️ cron 解析错误 [{cron_expr}]: {e}")

        time.sleep(1)


def print_status(publisher: EventDrivenPublisher):
    """打印系统状态"""
    login_state = publisher.operator.check_login_state()
    risk = publisher.risk_detector.state

    print("=" * 40)
    print("  📊 系统状态")
    print("=" * 40)
    print(f"  微信登录: {'✅ 已登录' if login_state.get('logged_in') else '❌ 未登录'}")
    print(f"  检测页面: {login_state.get('page', 'unknown')}")
    print(f"  风控等级: {risk.level.name}")
    print(f"  连续事件: {risk.consecutive_events}")
    if risk.cooldown_until > time.time():
        remaining = risk.cooldown_until - time.time()
        print(f"  ⏳ 冷却中: {remaining:.0f}s")
    print(f"  UIA 桥接: {'✅ 可用' if publisher.uia.available else '⚠️ 不可用'}")
    print("=" * 40)


# ═══════════════════════════════════════════════════════════════
# 自检
# ═══════════════════════════════════════════════════════════════

def run_self_test(publisher) -> int:
    """运行启动前自检，报告各项依赖和状态"""
    print("=" * 50)
    print("  🔍 系统自检")
    print("=" * 50)

    results = []

    # 1. Python 依赖
    checks = [
        ('opencv-python', 'cv2', 'OpenCV'),
        ('pyautogui', 'pyautogui', 'PyAutoGUI'),
        ('pywin32', 'win32gui', 'Windows API'),
        ('numpy', 'numpy', 'NumPy'),
        ('Pillow', 'PIL', 'Pillow'),
        ('PyYAML', 'yaml', 'YAML'),
    ]
    for _, mod, name in checks:
        try:
            __import__(mod)
            results.append(('✅', name, '已安装'))
        except ImportError:
            results.append(('❌', name, '未安装 — pip install'))

    # 2. OCR 引擎
    try:
        from paddleocr import PaddleOCR
        results.append(('✅', 'PaddleOCR', '可用'))
    except ImportError:
        results.append(('⚠️', 'PaddleOCR', '未安装 — 可选；微信原生 OCR 也不可用时需安装'))

    try:
        from src.locator.wechat_native_ocr import WeChatOCREngine
        engine = WeChatOCREngine()
        if engine.is_available:
            results.append(('✅', '微信 OCR', '可用'))
        else:
            results.append(('⚠️', '微信 OCR', 'WeChatOCR.exe 未找到，回退至 PaddleOCR'))
    except Exception:
        results.append(('⚠️', '微信 OCR', '初始化异常'))

    # 3. C# UIA 服务
    uia_exe = Path(__file__).parent / 'src' / 'cs_uia_service' / 'publish' / 'WeChatUIA.exe'
    if uia_exe.exists():
        results.append(('✅', 'C# UIA 服务', f'已编译 ({uia_exe.stat().st_size // 1024}KB)'))
    else:
        results.append(('⚠️', 'C# UIA 服务', '未编译 — 系统回退至纯 OCR 模式，部分功能受限'))

    # 4. .NET SDK
    import subprocess
    try:
        r = subprocess.run(['dotnet', '--version'], capture_output=True, text=True, timeout=5)
        results.append(('✅', '.NET SDK', r.stdout.strip()))
    except Exception:
        results.append(('⚠️', '.NET SDK', '未安装 — C# 服务无法编译'))

    # 5. 微信进程
    try:
        if WeChatWindowFinder.enum_all():
            results.append(('✅', '微信进程', '运行中'))
        else:
            results.append(('❌', '微信进程', '未找到 — 请启动微信'))
    except Exception:
        results.append(('⚠️', '微信进程', '无法检测'))

    # 6. 目录结构
    for d in ['templates/icons', 'logs']:
        p = Path(__file__).parent / d
        if p.exists():
            results.append(('✅', f'目录 {d}', '存在'))
        else:
            results.append(('⚠️', f'目录 {d}', '不存在'))

    # 7. 模板数量
    icons_dir = Path(__file__).parent / 'templates' / 'icons'
    png_count = len(list(icons_dir.glob('*.png'))) if icons_dir.exists() else 0
    if png_count > 0:
        results.append(('✅', '图标模板', f'{png_count} 个'))
    else:
        results.append(('⚠️', '图标模板', '0 个 — 首次运行时会自动生成'))

    # 打印报告
    for status, name, detail in results:
        print(f"  {status} {name:20s} {detail}")

    passed = sum(1 for s, _, _ in results if s == '✅')
    warnings = sum(1 for s, _, _ in results if s == '⚠️')
    errors = sum(1 for s, _, _ in results if s == '❌')

    print(f"\n  结果: {passed} 通过, {warnings} 警告, {errors} 错误")
    return 1 if errors > 0 else 0


def main():
    global _publisher_ref, _agent_ref

    configure_console_encoding()
    args = parse_args()

    if args.agent:
        from src.agent import PublisherAgentApp

        _agent_ref = PublisherAgentApp(config_path=args.agent_config)
        _agent_ref.run_forever(open_browser=not args.agent_no_browser)
        return 0

    # 纯空跑在创建发布器前结束，不加载 OCR，也不激活或操作微信窗口。
    if args.dry_run:
        if args.text:
            print(f"🔍 空跑模式：文案 '{args.text[:30]}...'，图片 {len(args.images or [])} 张")
            return 0
        if args.batch:
            tasks = batch_from_file(args.batch, confirm_publish=args.confirm_publish)
            for i, task in enumerate(tasks):
                print(f"  [{i+1}] {task.text[:40]}...")
            return 0
        print("❌ 空跑模式需要 --text 或 --batch")
        return 1

    # 初始化（单账号或多账号模式）
    publisher = None
    account_mgr = None
    windows = WeChatWindowFinder.enum_all()

    if args.accounts:
        if not windows:
            print("未发现运行中的微信窗口")
        else:
            print(f"发现 {len(windows)} 个微信窗口:\n")
            for hwnd, title in windows:
                info = WeChatWindowFinder.get_window_info(hwnd)
                if info:
                    display_name = f"{info.name} (PID={info.process_id})"
                    print(f"  📱 {display_name:30s} "
                          f"{'(最小化)' if info.is_minimized else ''}")
        return 0

    if args.account:
        account_mgr = AccountManager(bus=None)
        # 支持按 PID 指定账号
        if args.account.isdigit():
            hwnd = int(args.account)
            valid = WeChatWindowFinder.get_window_info(hwnd)
            if valid is None or not win32gui.IsWindow(hwnd):
                hwnd = None
        else:
            hwnd = WeChatWindowFinder.find_by_name(args.account)
        if hwnd is None:
            print(f"❌ 未找到账号 '{args.account}'")
            for hwnd_found, title in windows:
                info = WeChatWindowFinder.get_window_info(hwnd_found)
                print(f"  - {title} (PID={info.process_id if info else '?'})")
            return 1
        info = WeChatWindowFinder.get_window_info(hwnd)
        account_mgr.register(info)
        account_mgr.set_active(args.account)
        account_mgr.active.publisher.initialize()
        publisher = account_mgr.active.publisher
        _publisher_ref = publisher
    elif len(windows) > 1 and not args.accounts and not args.status:
        print(f"⚠️ 检测到 {len(windows)} 个微信窗口，请指定 --account <名称或PID>")
        for _, title in windows:
            print(f"  - {title}")
        print("\n提示: 使用 --accounts 查看详细信息 (含PID)")
        return 1
    else:
        publisher = EventDrivenPublisher(config_path=args.config)
        _publisher_ref = publisher

    try:
        # 查看状态模式（不需要完整初始化）
        if args.status:
            if publisher.operator.find_wechat_window():
                print_status(publisher)
            else:
                print("❌ 微信未运行")
            return 0

        # 初始化
        if not publisher.initialize():
            print("❌ 初始化失败，请检查微信是否启动且在前台")
            return 1

        # 恢复上次状态信息
        if args.resume:
            prev = load_state()
            if prev:
                print(f"📂 上次运行: {prev.get('saved_at', 'unknown')}")

        # 自检模式
        if args.test:
            return run_self_test(publisher)

        # 手动校准
        if args.calibrate:
            print("🔄 手动触发界面校准...")
            mapping = publisher.calibrator.calibrate(force=True)
            print(f"✅ 校准完成: {len(mapping.anchors)} 个锚点")
            return 0

        # 提取模板
        if args.extract_templates:
            print("🖼️ 提取图标模板...")
            from src.locator.template_extractor import update_all_templates
            count = update_all_templates(publisher.ocr)
            print(f"✅ 模板提取完成: {count} 个")
            return 0

        # 交互模式
        if args.interactive:
            interactive_mode(publisher)
            return 0

        # 定时调度模式
        if args.schedule:
            schedule_mode(publisher)
            return 0

        # 单次发布
        if args.text:
            task = PublishTask(
                text=args.text,
                images=args.images or [],
                confirm_publish=args.confirm_publish,
            )

            result = publisher.publish(task)
            if result.published:
                print(f"✅ 发布成功 (耗时 {result.elapsed_seconds:.0f}s)")
                return 0
            elif result.stopped_before_publish:
                print("🛑 内容已准备，安全停在发表前；请在微信中检查或手动取消")
                return 0
            else:
                print(f"❌ 发布失败: {result.error_message}")
                return 1

        # 批量发布
        if args.batch:
            tasks = batch_from_file(args.batch, confirm_publish=args.confirm_publish)
            print(f"📋 批量发布: {len(tasks)} 个任务")

            results = publisher.publish_batch(tasks)
            success_count = sum(1 for r in results if r.success)
            print(f"完成: {success_count}/{len(results)} 成功")

            # 保存状态
            save_state(publisher, results)

            return 0 if success_count == len(results) else 1

        # 无参数
        print("请指定 --text / --batch / --interactive / --schedule / --status")
        print("使用 --help 查看帮助")
        return 1

    finally:
        save_state(publisher)
        publisher.shutdown()


if __name__ == '__main__':
    sys.exit(main())
