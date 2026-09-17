// HTTP 封装与上传限制（fixture）

export const MAX_UPLOAD_MB = 5;

export interface UploadResult {
  fileId: string;
  sizeMb: number;
}

export class ApiClient {
  constructor(private baseUrl: string = "/api") {}

  async upload(file: File): Promise<UploadResult> {
    const sizeMb = file.size / (1024 * 1024);
    if (sizeMb > MAX_UPLOAD_MB) {
      throw new Error(`file too large: ${sizeMb.toFixed(1)}MB > ${MAX_UPLOAD_MB}MB`);
    }
    const form = new FormData();
    form.append("file", file);
    const resp = await fetch(`${this.baseUrl}/upload`, {
      method: "POST",
      body: form,
    });
    if (!resp.ok) {
      throw new Error(`upload failed: ${resp.status}`);
    }
    return (await resp.json()) as UploadResult;
  }
}
