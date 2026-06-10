#!/usr/bin/env python3
"""
PC 微信朋友圈自动化 —— 主入口

使用方式：
    # 单次发布
    python main.py --text "今天天气真好" --images photo1.jpg photo2.jpg

    # 从文件批量发布
    python main.py --batch posts.txt

    # 交互模式
    python main.py --interactive

配置文件：
    config/settings.yaml

Author: 版本无关微信自动化系统
"""

import sys
import argparse
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent))

from src.moments import MomentsPublisher, PublishTask


def parse_args():
    parser = argparse.ArgumentParser(
        description="PC 微信朋友圈自动化 —— 版本无关",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python main.py --text "今天天气真好"
  python main.py --text "分享照片" --images photo1.jpg photo2.jpg
  python main.py --batch posts.txt
  python main.py --interactive
        """,
    )

    parser.add_argument('--text', type=str, help='要发布的文字内容')
    parser.add_argument('--images', type=str, nargs='*', help='图片文件路径')
    parser.add_argument('--batch', type=str, help='批量发布文件（每行一个任务）')
    parser.add_argument('--config', type=str, help='配置文件路径')
    parser.add_argument('--interactive', action='store_true', help='交互模式')
    parser.add_argument('--dry-run', action='store_true', help='空跑（不实际发布）')

    return parser.parse_args()


def batch_from_file(filepath: str) -> list:
    """从文件读取批量发布任务"""
    tasks = []
    with open(filepath, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            # 格式：文字内容 | 图片路径1 图片路径2 ...
            parts = line.split('|')
            text = parts[0].strip()
            images = parts[1].strip().split() if len(parts) > 1 else []
            tasks.append(PublishTask(text=text, images=images))
    return tasks


def interactive_mode(publisher: MomentsPublisher):
    """交互模式"""
    print("\n" + "=" * 50)
    print("  微信朋友圈自动化 —— 交互模式")
    print("  输入文字后按 Enter 发布，输入 'quit' 退出")
    print("=" * 50 + "\n")

    while True:
        try:
            text = input("✏️  朋友圈内容: ").strip()

            if text.lower() in ('quit', 'exit', 'q'):
                print("再见 👋")
                break

            if not text:
                print("内容不能为空\n")
                continue

            # 可选图片
            img_input = input("🖼️  图片路径（可选，空格分隔，直接回车跳过）: ").strip()
            images = img_input.split() if img_input else []

            task = PublishTask(text=text, images=images)
            result = publisher.publish(task)

            if result.success:
                print(f"✅ 发布成功！(耗时 {result.elapsed_seconds:.0f}s)\n")
            else:
                print(f"❌ 发布失败: {result.error_message}\n")

        except KeyboardInterrupt:
            print("\n\n中断退出")
            break
        except Exception as e:
            print(f"❌ 错误: {e}")


def main():
    args = parse_args()

    # 初始化发布器
    publisher = MomentsPublisher(config_path=args.config)

    try:
        # 初始化
        if not publisher.initialize():
            print("初始化失败，请检查微信是否启动且在前台")
            return 1

        # 交互模式
        if args.interactive:
            interactive_mode(publisher)
            return 0

        # 单次发布
        if args.text:
            task = PublishTask(
                text=args.text,
                images=args.images or [],
            )

            if args.dry_run:
                print(f"空跑模式：将发布 '{task.text[:30]}...'")
                return 0

            result = publisher.publish(task)
            if result.success:
                print(f"✅ 发布成功 (耗时 {result.elapsed_seconds:.0f}s)")
                return 0
            else:
                print(f"❌ 发布失败: {result.error_message}")
                return 1

        # 批量发布
        if args.batch:
            tasks = batch_from_file(args.batch)
            print(f"批量发布: {len(tasks)} 个任务")

            if args.dry_run:
                for i, task in enumerate(tasks):
                    print(f"  [{i+1}] {task.text[:40]}...")
                return 0

            results = publisher.publish_batch(tasks)
            success_count = sum(1 for r in results if r.success)
            print(f"完成: {success_count}/{len(results)} 成功")
            return 0 if success_count == len(results) else 1

        # 无参数，显示帮助
        print("请指定 --text 或 --batch 或 --interactive")
        print("使用 --help 查看帮助")
        return 1

    finally:
        publisher.shutdown()


if __name__ == '__main__':
    sys.exit(main())
