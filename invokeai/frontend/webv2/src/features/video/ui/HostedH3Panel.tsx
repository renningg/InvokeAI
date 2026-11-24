/* oxlint-disable react-perf/jsx-no-new-function-as-prop -- the React Compiler memoizes field handlers, including those bound to image slots. */
import type { GalleryItem } from '@features/gallery';
import type { GenerateLora, ImageWithDims } from '@features/generation/contracts';
import type { HostedH3Settings, VideoWidgetValues } from '@features/video/core/types';

import { HStack, SimpleGrid, Stack, Switch, Text } from '@chakra-ui/react';
import { GalleryMediaSlot } from '@features/gallery/mediaSlot';
import { getDynamicPromptsConfig } from '@features/generation/settings';
import { openModelManager, useMiniMaxStatus, useModelsUi } from '@features/models';
import { createHostedH3ReferenceVideo, VIDEO_REFERENCE_MAX_IMAGES } from '@features/video/core/settings';
import {
  getHostedH3ReferenceVideoIssue,
  getHostedH3Models,
  getHostedH3MinDuration,
  getHostedH3Ratios,
  getHostedH3Resolutions,
  reconcileHostedH3Ratio,
} from '@features/video/core/videoPolicies';
import { Button } from '@platform/ui/Button';
import { Field } from '@platform/ui/Field';
import { useMemo } from 'react';
import { useTranslation } from 'react-i18next';

import { VideoPromptFields } from './VideoFormFields';
import { VideoFrameImageField } from './VideoFrameImageField';
import { VideoChoiceField } from './VideoModelField';
import { useVideoUi } from './VideoUiContext';

const VIDEO_ACCEPT = ['video'] as const;
const REFERENCE_SLOTS = [1, 2, 3, 4, 5, 6, 7, 8, 9];
const NO_LORAS: GenerateLora[] = [];

export const HostedH3Panel = ({ values, hosted }: { values: VideoWidgetValues; hosted: HostedH3Settings }) => {
  const { t } = useTranslation();
  const { findInGallery, notifications, patchValues, projectId, showPromptSyntaxHighlighting } = useVideoUi();
  const { canManageModels } = useModelsUi();
  const status = useMiniMaxStatus(true);
  const region = status.data?.region ?? null;
  const models = getHostedH3Models(region);
  const resolutions = getHostedH3Resolutions(hosted.model, region);
  const ratios = getHostedH3Ratios(hosted);
  const images = values.hostedReferenceImages ?? [];
  const selectedVideo = values.hostedReferenceVideo;
  const videoLabel = t('widgets.video.hosted.referenceVideo');
  const videoSlotLabels = useMemo(
    () => ({
      choose: t('widgets.video.hosted.chooseSlot', { slot: videoLabel }),
      remove: t('widgets.video.hosted.removeSlot', { slot: videoLabel }),
      replace: t('widgets.video.hosted.replaceSlot', { slot: videoLabel, name: selectedVideo?.video_name }),
    }),
    [selectedVideo?.video_name, t, videoLabel]
  );
  const videoSlotValue = useMemo(
    () =>
      selectedVideo
        ? {
            kind: 'video' as const,
            name: selectedVideo.video_name,
            width: selectedVideo.width,
            height: selectedVideo.height,
          }
        : null,
    [selectedVideo]
  );
  const changeVideo = (item: GalleryItem | null) => {
    if (item && item.kind !== 'video') {
      return;
    }
    const video = item ? createHostedH3ReferenceVideo(item) : null;
    const issue = video && getHostedH3ReferenceVideoIssue(video, item?.mediaOrigin);
    if (issue) {
      notifications.info(issue);
      return;
    }
    patchValues((current) =>
      current.hostedH3?.input === 'references' && current.hostedReferenceVideo?.video_name === selectedVideo?.video_name
        ? { hostedReferenceVideo: video }
        : {}
    );
  };
  const setOptions = (changes: Partial<HostedH3Settings>) =>
    patchValues((current) => {
      if (!current.hostedH3) {
        return {};
      }
      const next = { ...current.hostedH3, ...changes };
      if (changes.model || changes.input) {
        next.duration = Math.max(next.duration, getHostedH3MinDuration(next.model));
        if (!getHostedH3Resolutions(next.model, region).includes(next.resolution)) {
          next.resolution = '768P';
        }
        return { hostedH3: reconcileHostedH3Ratio(next) };
      }
      return { hostedH3: next };
    });
  const setFrame = (slot: 'firstFrameImage' | 'lastFrameImage', image: ImageWithDims | null) =>
    patchValues((current) => {
      if (current.hostedH3?.input !== 'frames') {
        return {};
      }
      return { [slot]: image, ...(image ? { references: [], sourceVideo: null, conditioningClip: null } : {}) };
    });
  const changeReference = (index: number, original: string | null, image: ImageWithDims | null) =>
    patchValues((current) => {
      if (current.hostedH3?.input !== 'references') {
        return {};
      }
      const next = [...(current.hostedReferenceImages ?? [])];
      if (original === null) {
        if (!image || next.length >= VIDEO_REFERENCE_MAX_IMAGES) {
          return {};
        }
        next.push(image);
      } else {
        if (next[index]?.image_name !== original) {
          return {};
        }
        if (image) {
          next[index] = image;
        } else {
          next.splice(index, 1);
        }
      }
      return { hostedReferenceImages: next };
    });
  const minimum = getHostedH3MinDuration(hosted.model);
  const hasModel = models.some((id) => id === hosted.model);
  const activeRatio = hosted.input === 'frames' ? 'adaptive' : hosted.ratio;
  return (
    <Stack gap="2">
      <Stack gap="2" px="2" pt="1">
        <Text color="fg.muted" fontSize="sm">
          {t('widgets.video.hosted.billing')}
        </Text>
        <HStack flexWrap="wrap" justify="space-between">
          <Text color={status.data?.configured && region ? 'fg.muted' : 'fg.warning'} fontSize="sm" role="status">
            {status.isPending
              ? t('widgets.video.hosted.checking')
              : status.isError
                ? t('widgets.video.hosted.statusFailed')
                : !status.data?.configured
                  ? t('widgets.video.hosted.keyNeeded')
                  : !region
                    ? t('widgets.video.hosted.regionNeeded')
                    : t('widgets.video.hosted.connected', {
                        region: t(region === 'cn' ? 'models.miniMaxChina' : 'models.miniMaxGlobal'),
                      })}
          </Text>
          {status.isError && (
            <Button size="sm" variant="outline" onClick={() => void status.refetch()}>
              {t('common.retry')}
            </Button>
          )}
          {canManageModels && (
            <Button size="sm" variant="outline" onClick={() => openModelManager({ projectId, providerId: 'minimax' })}>
              {t('widgets.video.hosted.configure')}
            </Button>
          )}
        </HStack>
        <VideoChoiceField
          label={t('widgets.video.mainModel')}
          value={hosted.model}
          onChange={(model) => setOptions({ model })}
        >
          {!hasModel && (
            <option disabled value={hosted.model}>
              {t('widgets.video.hosted.unavailable', { value: hosted.model })}
            </option>
          )}
          {models.map((id) => (
            <option key={id}>{id}</option>
          ))}
        </VideoChoiceField>
      </Stack>
      <VideoPromptFields
        batchCount={values.batchCount}
        dynamicPrompts={getDynamicPromptsConfig(values)}
        expandPromptSuggestion={null}
        isSeedFixed={false}
        loras={NO_LORAS}
        model={null}
        negativePrompt=""
        negativePromptEnabled={false}
        negativePromptHeightPx={values.negativePromptHeightPx}
        negativeVisible={false}
        positivePrompt={values.positivePrompt}
        positivePromptHeightPx={values.positivePromptHeightPx}
        projectId={projectId}
        showSyntaxHighlighting={showPromptSyntaxHighlighting}
        onPatchValues={patchValues}
      />
      <Stack gap="2" px="2" pb="3">
        <VideoChoiceField
          label={t('widgets.video.hosted.inputs')}
          value={hosted.input}
          onChange={(value) => setOptions({ input: value === 'frames' || value === 'references' ? value : 'text' })}
        >
          <option value="text">{t('widgets.video.hosted.text')}</option>
          <option value="frames">{t('widgets.video.hosted.frames')}</option>
          <option value="references">{t('widgets.video.hosted.references')}</option>
        </VideoChoiceField>
        {hosted.input === 'frames' && (
          <SimpleGrid columns={2} gap="2">
            <Field label={t('widgets.video.firstFrame')}>
              <VideoFrameImageField
                dropId="hosted-h3-first"
                slotLabel={t('widgets.video.firstFrame')}
                dropLabel={t('widgets.video.dropFirstFrame')}
                image={values.firstFrameImage}
                onChange={(image) => setFrame('firstFrameImage', image)}
              />
            </Field>
            <Field label={t('widgets.video.lastFrame')}>
              <VideoFrameImageField
                dropId="hosted-h3-last"
                slotLabel={t('widgets.video.lastFrame')}
                dropLabel={t('widgets.video.dropLastFrame')}
                image={values.lastFrameImage}
                onChange={(image) => setFrame('lastFrameImage', image)}
              />
            </Field>
          </SimpleGrid>
        )}
        {hosted.input === 'references' && (
          <Stack gap="2">
            <Field label={videoLabel} helpText={t('widgets.video.hosted.referenceVideoBilling')}>
              <GalleryMediaSlot
                accept={VIDEO_ACCEPT}
                galleryOnly
                dropId="hosted-h3-reference-video"
                labels={videoSlotLabels}
                value={videoSlotValue}
                onChange={changeVideo}
                onFind={
                  selectedVideo ? () => findInGallery({ kind: 'video', name: selectedVideo.video_name }) : undefined
                }
              />
            </Field>
            {values.references.some((entry) => entry.kind === 'video') && (
              <Text color="fg.muted" fontSize="sm">
                {t('widgets.video.hosted.localReferenceClips')}
              </Text>
            )}
            {REFERENCE_SLOTS.slice(0, images.length + 1).map((number) => {
              const index = number - 1;
              const image = images[index];
              const name = image?.image_name ?? null;
              const slotLabel = t('widgets.video.hosted.reference', { n: number });
              // A positional key keeps the focused chooser mounted when this image changes or its successor moves up.
              return (
                <Field key={number} label={image ? slotLabel : t('widgets.video.hosted.addReference')}>
                  <VideoFrameImageField
                    dropId={`hosted-h3-ref-${number}`}
                    dropLabel={t('widgets.video.dropReference')}
                    slotLabel={slotLabel}
                    image={image ?? null}
                    onChange={(image) => changeReference(index, name, image)}
                  />
                </Field>
              );
            })}
          </Stack>
        )}
        <SimpleGrid columns={2} gap="2">
          <VideoChoiceField
            label={t('widgets.video.targetResolution')}
            value={hosted.resolution}
            onChange={(resolution) => setOptions({ resolution })}
          >
            {!resolutions.includes(hosted.resolution) && (
              <option disabled value={hosted.resolution}>
                {t('widgets.video.hosted.unavailable', { value: hosted.resolution })}
              </option>
            )}
            {resolutions.map((id) => (
              <option key={id}>{id}</option>
            ))}
          </VideoChoiceField>
          <VideoChoiceField
            label={t('widgets.video.hosted.duration')}
            value={String(hosted.duration)}
            onChange={(value) => setOptions({ duration: Number(value) })}
          >
            {Array.from({ length: 16 - minimum }, (_, i) => (
              <option key={i + minimum} value={i + minimum}>
                {i + minimum}
              </option>
            ))}
          </VideoChoiceField>
        </SimpleGrid>
        <VideoChoiceField
          label={t('widgets.video.aspectRatio')}
          value={activeRatio}
          onChange={(ratio) => setOptions({ ratio })}
        >
          {ratios.map((ratio) => (
            <option key={ratio} value={ratio}>
              {ratio === 'adaptive' ? t('widgets.video.hosted.adaptive') : ratio}
            </option>
          ))}
        </VideoChoiceField>
        <Switch.Root
          checked={hosted.generateAudio}
          onCheckedChange={({ checked }) => setOptions({ generateAudio: checked })}
        >
          <Switch.HiddenInput />
          <Switch.Control>
            <Switch.Thumb />
          </Switch.Control>
          <Switch.Label>{t('widgets.video.hosted.audio')}</Switch.Label>
        </Switch.Root>
      </Stack>
    </Stack>
  );
};
