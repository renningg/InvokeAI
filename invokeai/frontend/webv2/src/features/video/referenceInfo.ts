import type { components } from '@api/schema';
import type { HostedH3ReferenceVideo } from '@features/video/core/types';

import { getHostedH3ReferenceVideoIssue } from '@features/video/core/videoPolicies';
import { apiFetchJson } from '@platform/transport/http';

/** The Gallery lists rounded playback timings and disk usage that may include thumbnails and sidecars. */
export const inspectHostedH3ReferenceVideo = async (
  reference: HostedH3ReferenceVideo,
  signal: AbortSignal,
  mediaOrigin?: string
): Promise<HostedH3ReferenceVideo> => {
  signal.throwIfAborted();
  const info = await apiFetchJson<components['schemas']['VideoReferenceInfo']>(
    `/api/v1/videos/i/${encodeURIComponent(reference.video_name)}/reference-info`,
    { signal }
  );
  if (info.file_size_bytes > 50_000_000) {
    throw new Error('MiniMax reference videos must be 50 MB or smaller.');
  }
  if (!(info.file_size_bytes > 0) || typeof info.duration !== 'number' || typeof info.fps !== 'number') {
    throw new Error('Could not read the reference video’s duration and frame rate. Choose another Gallery video.');
  }
  const measured = { ...reference, durationSeconds: info.duration, fps: info.fps };
  const issue = getHostedH3ReferenceVideoIssue(measured, mediaOrigin);
  if (issue) {
    throw new Error(issue);
  }
  return measured;
};
