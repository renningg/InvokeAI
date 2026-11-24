import type { GalleryItemRef, GalleryVideoItem } from '@features/gallery';
import type { VideoWidgetValues } from '@features/video/core/types';
import type { VideoSection } from '@features/video/core/videoPolicies';
import type { AccountScope } from '@platform/state/accountLifecycle';
import type { ReactNode } from 'react';

import { createContext, use, useMemo, useSyncExternalStore } from 'react';

// A choice belongs to its account and project even when React hides its panel in an Activity.
interface PendingReferenceSelection {
  controller: AbortController;
  videoName: string;
}
const pendingSelections = new WeakMap<AccountScope, Map<string, PendingReferenceSelection>>();
const selectionListeners = new Set<() => void>();
const subscribeToSelections = (listener: () => void) => {
  selectionListeners.add(listener);
  return () => {
    selectionListeners.delete(listener);
  };
};
const notifySelections = () => selectionListeners.forEach((listener) => listener());

export const hasPendingReferenceSelection = (owner: AccountScope, projectId: string) =>
  pendingSelections.get(owner)?.has(projectId) === true;

export const usePendingReferenceName = (owner: AccountScope, projectId: string) =>
  useSyncExternalStore(subscribeToSelections, () => pendingSelections.get(owner)?.get(projectId)?.videoName ?? null);

export const cancelReferenceSelection = (owner: AccountScope, projectId: string) => {
  pendingSelections.get(owner)?.get(projectId)?.controller.abort();
};

export const beginReferenceSelection = (owner: AccountScope, projectId: string, videoName: string) => {
  const pending = pendingSelections.get(owner) ?? new Map<string, PendingReferenceSelection>();
  pendingSelections.set(owner, pending);
  const previous = pending.get(projectId);
  const controller = new AbortController();
  const selection = { controller, videoName };
  pending.set(projectId, selection);
  previous?.controller.abort();
  const signal = AbortSignal.any([controller.signal, owner.signal]);
  const isCurrent = () => !signal.aborted && pending.get(projectId) === selection;
  const finish = () => {
    if (pending.get(projectId) === selection) {
      pending.delete(projectId);
      notifySelections();
    }
  };
  signal.addEventListener('abort', finish, { once: true });
  notifySelections();
  return {
    signal,
    isCurrent,
    finish: () => {
      signal.removeEventListener('abort', finish);
      finish();
    },
  };
};

/** The persisted open state of each Video section, kept with Generate's under the same per-user preference. */
export const VIDEO_SECTION_IDS = {
  components: 'video-components',
  concepts: 'video-concepts',
  conditioningClip: 'video-conditioning-clip',
  dimensions: 'video-dimensions',
  frames: 'video-frames',
  guidance: 'video-guidance',
  references: 'video-references',
  render: 'video-render',
  source: 'video-source',
} as const satisfies Record<VideoSection, string>;

/** Workbench supplies this UI port because features cannot import workbench. */
export interface VideoUiAdapter {
  /** Reveal this media's board, page, and cell in Gallery and show Preview. */
  findInGallery(ref: GalleryItemRef): void;
  /**
   * Where an upload made from the panel lands, resolved when the upload starts from the listing of every readable
   * board: the Gallery's auto-add board while that listing lets the account add to it, else the project's inbox; `none`
   * is Uncategorized.
   */
  getUploadBoardId(): Promise<string>;
  /** Notices about this project; `undoable` reports a change that already happened, with its Undo. */
  notifications: {
    info(title: string): void;
    undoable(title: string, notice: { onUndo: () => void; supersedes: string }): void;
  };
  /** Functional patches run synchronously against the originating project's latest normalized values. */
  patchValues(
    values: Partial<VideoWidgetValues> | ((current: VideoWidgetValues) => Partial<VideoWidgetValues>),
    origin?: 'user' | 'system'
  ): void;
  /**
   * Select item in Preview and request looping this window. Returns its playback token, or null if Preview could not be
   * raised and no request was made.
   */
  playVideoSpanInPreview(span: { endSeconds: number; item: GalleryVideoItem; startSeconds: number }): number | null;
  projectId: string;
  rawValues: Record<string, unknown>;
  /** Read this project's raw panel values at call time without subscribing, for writes that complete asynchronously. */
  readValues(): Record<string, unknown>;
  /** A board refused an upload or is gone: read the listing uploads are routed by again, at once. */
  refreshBoardDestinations(): void;
  reportError(message: string): void;
  showPromptSyntaxHighlighting: boolean;
  touchGalleryImages(): void;
  /** What Preview is doing with the last span it was asked to play, for the button that asked. */
  videoSpanPlayback: VideoSpanPlaybackPort;
}

/**
 * Reports the armed request token, or null after disarming. isPlaying mirrors the element; pause preserves the armed
 * window for native resume.
 */
export interface VideoSpanPlaybackState {
  isPlaying: boolean;
  pause(): void;
  token: number;
}

export interface VideoSpanPlaybackPort {
  getState(): VideoSpanPlaybackState | null;
  subscribe(listener: () => void): () => void;
}

/** The adapter's callbacks, which are stable for the lifetime of a project. */
export type VideoUiActions = Pick<
  VideoUiAdapter,
  | 'findInGallery'
  | 'getUploadBoardId'
  | 'notifications'
  | 'patchValues'
  | 'playVideoSpanInPreview'
  | 'readValues'
  | 'refreshBoardDestinations'
  | 'reportError'
  | 'touchGalleryImages'
  | 'videoSpanPlayback'
>;

const VideoUiContext = createContext<VideoUiAdapter | null>(null);
/** Separate actions from the value-changing adapter so action-only consumers do not rerender on form edits. */
const VideoUiActionsContext = createContext<VideoUiActions | null>(null);

export const VideoUiProvider = ({ adapter, children }: { adapter: VideoUiAdapter; children: ReactNode }) => {
  const {
    findInGallery,
    getUploadBoardId,
    notifications,
    patchValues,
    playVideoSpanInPreview,
    readValues,
    refreshBoardDestinations,
    reportError,
    touchGalleryImages,
    videoSpanPlayback,
  } = adapter;
  const actions = useMemo<VideoUiActions>(
    () => ({
      findInGallery,
      getUploadBoardId,
      notifications,
      patchValues,
      playVideoSpanInPreview,
      readValues,
      refreshBoardDestinations,
      reportError,
      touchGalleryImages,
      videoSpanPlayback,
    }),
    [
      findInGallery,
      getUploadBoardId,
      notifications,
      patchValues,
      playVideoSpanInPreview,
      readValues,
      refreshBoardDestinations,
      reportError,
      touchGalleryImages,
      videoSpanPlayback,
    ]
  );

  return (
    <VideoUiActionsContext value={actions}>
      <VideoUiContext value={adapter}>{children}</VideoUiContext>
    </VideoUiActionsContext>
  );
};

export const useVideoUi = (): VideoUiAdapter => {
  const adapter = use(VideoUiContext);

  if (!adapter) {
    throw new Error('Video UI requires an App-composed VideoUiProvider.');
  }

  return adapter;
};

export const useVideoUiActions = (): VideoUiActions => {
  const actions = use(VideoUiActionsContext);

  if (!actions) {
    throw new Error('Video UI requires an App-composed VideoUiProvider.');
  }

  return actions;
};
