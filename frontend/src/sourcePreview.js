import { youtubeLinks } from './project.js';

export function sourcePreviewLinks(url, segment) {
  const [watch] = youtubeLinks([url]);
  if (!watch) return null;
  const id = new URL(watch).searchParams.get('v');
  const embed = new URL('https://www.youtube-nocookie.com/embed/' + id);
  embed.searchParams.set('autoplay', '1');
  embed.searchParams.set('playsinline', '1');
  let href = watch;
  if (segment) {
    const { start_seconds: start, end_seconds: end } = segment;
    if (!Number.isSafeInteger(start) || start < 0 || !Number.isSafeInteger(end) || end <= start) return null;
    embed.searchParams.set('start', start);
    embed.searchParams.set('end', end);
    href += '&t=' + start + 's';
  }
  return { embed: embed.href, href };
}
