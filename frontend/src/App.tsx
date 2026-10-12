import { useState, useRef, useCallback } from "react";
import "./App.css";

const ERROR_MESSAGES: Record<number, string> = {
  400: "Invalid request. Please attach a valid video clip.",
  401: "Service authentication failed.",
  403: "Session is not live. Please try again later.",
  413: "A segment exceeded the size limit. Try a shorter or lower-bitrate video.",
  422: "Unsupported format. Only valid MP4 videos are accepted.",
  429: "Too many requests. Please wait and retry.",
  503: "Service temporarily unavailable. Please retry shortly.",
};

function App() {
  const [file, setFile] = useState<File | null>(null);
  const [originalUrl, setOriginalUrl] = useState<string | null>(null);
  const [transformedUrl, setTransformedUrl] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadingSample, setLoadingSample] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dragActive, setDragActive] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const clearPreviousUrls = useCallback(() => {
    if (originalUrl) URL.revokeObjectURL(originalUrl);
    if (transformedUrl) URL.revokeObjectURL(transformedUrl);
    setOriginalUrl(null);
    setTransformedUrl(null);
  }, [originalUrl, transformedUrl]);

  const handleFile = useCallback(
    (selected: File) => {
      setError(null);
      clearPreviousUrls();

      if (selected.type !== "video/mp4") {
        setError("Only MP4 files are accepted.");
        return;
      }

      setFile(selected);
      setOriginalUrl(URL.createObjectURL(selected));
    },
    [clearPreviousUrls]
  );

  const onFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const selected = e.target.files?.[0];
    if (selected) handleFile(selected);
  };

  const onDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragActive(false);
    const selected = e.dataTransfer.files?.[0];
    if (selected) handleFile(selected);
  };

  const onDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    setDragActive(true);
  };

  const onDragLeave = () => setDragActive(false);

  const loadSample = async () => {
    setError(null);
    setLoadingSample(true);
    clearPreviousUrls();
    try {
      const resp = await fetch("/api/sample");
      if (!resp.ok) throw new Error("Failed to load sample video.");
      const blob = await resp.blob();
      const sampleFile = new File([blob], "short.mp4", { type: "video/mp4" });
      setFile(sampleFile);
      setOriginalUrl(URL.createObjectURL(sampleFile));
    } catch {
      setError("Could not load sample video.");
    } finally {
      setLoadingSample(false);
    }
  };

  const transformVideo = async () => {
    if (!file) return;
    setError(null);
    setLoading(true);
    if (transformedUrl) {
      URL.revokeObjectURL(transformedUrl);
      setTransformedUrl(null);
    }

    try {
      const formData = new FormData();
      formData.append("clip", file);

      const resp = await fetch("/api/transform", { method: "POST", body: formData });

      if (!resp.ok) {
        let msg = ERROR_MESSAGES[resp.status] || `Transform failed (${resp.status}).`;
        try {
          const body = await resp.json();
          if (body.detail) msg = body.detail;
        } catch { /* use default */ }
        setError(msg);
        return;
      }

      const blob = await resp.blob();
      setTransformedUrl(URL.createObjectURL(blob));
    } catch {
      setError("Network error. Please check your connection.");
    } finally {
      setLoading(false);
    }
  };

  const formatSize = (bytes: number) => {
    if (bytes < 1_000_000) return `${(bytes / 1_000).toFixed(0)} KB`;
    return `${(bytes / 1_000_000).toFixed(1)} MB`;
  };

  return (
    <div className="app">
      <header className="header">
        <h1>Video Grayscale Transformer</h1>
        <p>Upload an MP4 video of any size — it will be chunked and transformed automatically</p>
      </header>

      <div className="controls">
        <div
          className={`dropzone ${dragActive ? "active" : ""}`}
          onDrop={onDrop}
          onDragOver={onDragOver}
          onDragLeave={onDragLeave}
          onClick={() => fileInputRef.current?.click()}
        >
          <input
            ref={fileInputRef}
            type="file"
            accept="video/mp4"
            onChange={onFileChange}
            hidden
          />
          {file ? (
            <p className="file-name">{file.name} ({formatSize(file.size)})</p>
          ) : (
            <p>Drag & drop an MP4 here, or click to browse</p>
          )}
        </div>

        <div className="buttons">
          <button onClick={loadSample} disabled={loadingSample || loading} className="btn secondary">
            {loadingSample ? "Loading..." : "Load Sample"}
          </button>
          <button onClick={transformVideo} disabled={!file || loading} className="btn primary">
            {loading ? "Transforming..." : "Transform to Grayscale"}
          </button>
        </div>
      </div>

      {error && <div className="error">{error}</div>}

      {loading && (
        <div className="spinner-container">
          <div className="spinner" />
          <p>Processing your video — splitting, transforming, and reassembling...</p>
        </div>
      )}

      {(originalUrl || transformedUrl) && (
        <div className="videos">
          {originalUrl && (
            <div className="video-card">
              <h3>Original</h3>
              <video src={originalUrl} controls />
            </div>
          )}
          {transformedUrl && (
            <div className="video-card">
              <h3>Grayscale</h3>
              <video src={transformedUrl} controls />
              <a
                href={transformedUrl}
                download="transformed.mp4"
                className="btn secondary download-btn"
              >
                Download
              </a>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default App;
