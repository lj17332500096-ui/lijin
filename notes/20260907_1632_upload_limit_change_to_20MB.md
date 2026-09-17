# 上传文件大小限制修改为 20MB

## 需要修改的文件

### 1. my_creative_agent/tools.py（第781行）
```python
# 原代码：
if size > 2 * 1024 * 1024:
    return f"错误：文件超过 2MB（实际 {size} 字节），暂不支持整读。"

# 修改为：
if size > 20 * 1024 * 1024:
    return f"错误：文件超过 20MB（实际 {size} 字节），暂不支持整读。"
```

### 2. my_creative_agent/runtime/sandbox_snapshot.py（第15行）
```python
# 原代码：
MAX_FILE_BYTES = 2 * 1024 * 1024

# 修改为：
MAX_FILE_BYTES = 20 * 1024 * 1024
```

### 3. webapp.py 已确认
```python
# 第63行已是 20MB，无需修改：
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
```

## 验证方法
修改后重启服务，尝试上传超过2MB的文件，应能成功上传至20MB。

## 备注
- 资料上传 API（/api/artifacts/upload）已经是20MB限制
- 这是工作区文件读取和沙箱快照的限制，不是浏览器端上传限制
