// Runtime validation coverage for: IncuBrix API requests and job retrieval.
import { afterEach, describe, expect, it, vi } from "vitest";
import { API_URL, fileUrl, getJob, getVoices, submitJob } from "./api.js";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("IncuBrix API client", () => {
  it("loads voices from the backend", async () => {
    const voices = [{ id: "voice-1", name: "Studio voice" }];
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ voices }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(getVoices()).resolves.toEqual(voices);
    expect(fetchMock).toHaveBeenCalledWith(`${API_URL}/api/v1/voices`, expect.objectContaining({
      signal: expect.any(AbortSignal),
    }));
  });

  it("submits a JSON generation request and returns the real queued job", async () => {
    const job = { job_id: "job-123", status: "queued", progress: 0 };
    const request = { script: "A story begins.", aspect_ratios: ["16:9"] };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => job,
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(submitJob(request)).resolves.toEqual(job);
    expect(fetchMock).toHaveBeenCalledWith(`${API_URL}/api/v1/broll/jobs`, expect.objectContaining({
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(request),
    }));
  });

  it("surfaces backend job errors instead of a success-shaped fallback", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: false,
      status: 404,
      json: async () => ({ error: { message: "Job was not found." } }),
    }));

    await expect(getJob("missing-job")).rejects.toThrow("Job was not found.");
  });

  it("resolves generated file paths against the configured API origin", () => {
    expect(fileUrl("/api/v1/jobs/job-123/files/video_16x9.mp4"))
      .toBe(`${API_URL}/api/v1/jobs/job-123/files/video_16x9.mp4`);
  });
});
