import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import App from './App.tsx';
import { WorkspaceErrorBoundary } from './components/shared/WorkspaceErrorBoundary';
import 'leaflet/dist/leaflet.css';
import './index.css';

const rootElement = document.getElementById('root');

if (!rootElement) {
  throw new Error('CrossFlow AI could not find the root application element.');
}

createRoot(rootElement).render(
  <StrictMode>
    <WorkspaceErrorBoundary>
      <App />
    </WorkspaceErrorBoundary>
  </StrictMode>,
);
