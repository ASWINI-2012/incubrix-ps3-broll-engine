import { describe, expect, it } from "vitest";
import { getCurrentJobVideo, getCurrentJobVideos } from "./video.js";

describe("current job video selection", () => {
  it("shows only the MP4 linked to the latest completed job", () => {
    const latest = {
      job_id: "latest-job",
      status: "completed",
      result: {
        videos: [
          { file: "old.mp4", url: "/api/v1/jobs/previous-job/files/old.mp4", width: 1920, height: 1080 },
          { file: "current.mp4", url: "/api/v1/jobs/latest-job/files/current.mp4", width: 1920, height: 1080 },
          { file: "manifest.json", url: "/api/v1/jobs/latest-job/files/manifest.json", width: 1920, height: 1080 },
        ],
      },
    };

    expect(getCurrentJobVideos(latest).map((video) => video.file)).toEqual(["current.mp4"]);
    expect(getCurrentJobVideo(latest, "16:9").file).toBe("current.mp4");
  });

  it("hides previous output while the latest explicit request is queued, running, or failed", () => {
    for (const status of ["queued", "processing", "failed"]) {
      const latest = { job_id: "new-job", status, result: null };
      expect(getCurrentJobVideos(latest)).toEqual([]);
      expect(getCurrentJobVideo(latest, "16:9")).toBeNull();
    }
  });
});
