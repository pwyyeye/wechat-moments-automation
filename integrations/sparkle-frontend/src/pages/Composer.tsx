/**
 * Composer — 朋友圈编辑发布页面
 */

import React, { useState, useCallback } from "react";
import {
  Card, CardBody, CardHeader, Textarea, Button, Chip, Divider,
  addToast,
} from "@heroui/react";
import { publish } from "../api/client";
import { FiSend, FiImage, FiX } from "react-icons/fi";

export default function Composer() {
  const [text, setText] = useState("");
  const [images, setImages] = useState<string[]>([]);
  const [publishing, setPublishing] = useState(false);

  const handlePublish = useCallback(async () => {
    if (!text.trim() && images.length === 0) return;

    setPublishing(true);
    try {
      const result = await publish({ text: text.trim(), images });
      if (result.success) {
        addToast({ title: "发布成功", description: `耗时 ${result.elapsed_seconds.toFixed(1)}s`, color: "success" });
        setText("");
        setImages([]);
      } else {
        addToast({ title: "发布失败", description: result.error, color: "danger" });
      }
    } catch (e: any) {
      addToast({ title: "请求失败", description: e.message, color: "danger" });
    }
    setPublishing(false);
  }, [text, images]);

  const addImage = () => {
    // Electron dialog to select files
    if (window.electron) {
      window.electron.openFileDialog().then((paths: string[]) => {
        setImages((prev) => [...prev, ...paths].slice(0, 9));
      });
    }
  };

  return (
    <div className="p-6 max-w-3xl mx-auto space-y-6">
      <h1 className="text-2xl font-bold">编辑朋友圈</h1>

      <Card>
        <CardBody className="p-6 space-y-4">
          {/* 文字输入 */}
          <Textarea
            label="朋友圈文字"
            placeholder="这一刻的想法..."
            value={text}
            onValueChange={setText}
            minRows={3}
            maxRows={8}
            maxLength={2000}
            description={`${text.length}/2000`}
          />

          {/* 图片列表 */}
          {images.length > 0 && (
            <div>
              <div className="text-sm text-default-500 mb-2">
                图片 ({images.length}/9)
              </div>
              <div className="flex gap-2 flex-wrap">
                {images.map((path, i) => (
                  <Chip
                    key={i}
                    onClose={() => setImages((prev) => prev.filter((_, j) => j !== i))}
                    variant="flat"
                    size="sm"
                    startContent={<FiImage size={12} />}
                  >
                    {path.split("\\").pop()}
                  </Chip>
                ))}
              </div>
            </div>
          )}

          <Divider />

          {/* 操作按钮 */}
          <div className="flex gap-3">
            <Button
              color="primary"
              startContent={<FiSend />}
              onPress={handlePublish}
              isLoading={publishing}
              isDisabled={!text.trim() && images.length === 0}
            >
              发布
            </Button>
            <Button
              variant="flat"
              startContent={<FiImage />}
              onPress={addImage}
            >
              添加图片
            </Button>
          </div>
        </CardBody>
      </Card>
    </div>
  );
}

// 扩展 window 类型（Electron preload 暴露的 API）
declare global {
  interface Window {
    electron?: {
      openFileDialog: () => Promise<string[]>;
    };
  }
}
