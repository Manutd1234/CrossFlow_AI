import { Component, type ReactNode } from 'react';

interface WorkspaceErrorBoundaryProps {
  children: ReactNode;
}

interface WorkspaceErrorBoundaryState {
  failed: boolean;
}

export class WorkspaceErrorBoundary extends Component<WorkspaceErrorBoundaryProps, WorkspaceErrorBoundaryState> {
  state: WorkspaceErrorBoundaryState = { failed: false };

  static getDerivedStateFromError(): WorkspaceErrorBoundaryState {
    return { failed: true };
  }

  render(): ReactNode {
    if (this.state.failed) {
      return <main className="glass-panel app-loading-panel" role="alert" aria-labelledby="workspace-error-title">
        <h1 id="workspace-error-title">Unable to load the workspace</h1>
        <p>Check your connection, then reload to get the latest version of CrossFlow.</p>
        <button type="button" className="ui-button-primary" onClick={() => location.reload()}>
          Reload workspace
        </button>
      </main>;
    }

    return this.props.children;
  }
}
