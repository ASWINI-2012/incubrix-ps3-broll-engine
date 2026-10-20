export function outputRatio(video) {
  const width = Number(video?.width);
  const height = Number(video?.height);
  if (!width || !height) return null;
  const ratio = width / height;
  if (Math.abs(ratio - 16 / 9) < 0.02) return "16:9";
  if (Math.abs(ratio - 9 / 16) < 0.02) return "9:16";
  if (Math.abs(ratio - 1) < 0.02) return "1:1";
  return `${width}:${height}`;
}

export function getCurrentJobVideos(job) {
  if (job?.status !== "completed" || !job?.job_id || !Array.isArray(job?.result?.videos)) return [];
  const jobFilesPath = `/api/v1/jobs/${encodeURIComponent(job.job_id)}/files/`;
  return job.result.videos.filter((video) => {
    if (typeof video?.file !== "string" || !video.file.toLowerCase().endsWith(".mp4") || typeof video?.url !== "string") return false;
    try {
      return new URL(video.url, "http://studio.local").pathname.startsWith(jobFilesPath);
    } catch {
      return false;
    }
  });
}

export function getCurrentJobVideo(job, ratio) {
  const videos = getCurrentJobVideos(job);
  return videos.find((video) => outputRatio(video) === ratio) || videos[0] || null;
}
