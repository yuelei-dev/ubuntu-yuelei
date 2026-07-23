export const VideoPreview = ({previewUrl}: {previewUrl?: string}) => <section aria-labelledby="preview-heading">
  <h2 id="preview-heading">Preview</h2>
  {previewUrl
    ? <video aria-label="Project preview" controls preload="metadata" src={previewUrl}/>
    : <p role="status">Preview will be available after rendering.</p>}
</section>;
