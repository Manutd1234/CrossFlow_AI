import { CarFront, LogIn, ShieldCheck } from 'lucide-react';
import type { AuthSession } from '../../types';
import { ICON_SIZE } from '../../theme/iconSizes';
import './WorkspaceAccess.css';

interface WorkspaceAccessProps {
  identity: AuthSession | null;
  driverAccess: boolean;
  signInAvailable: boolean;
  onOpenAccount: () => void;
}

/** Explain the active workspace without treating a UI switch as authorization. */
export function WorkspaceAccess({ identity, driverAccess, signInAvailable, onOpenAccount }: WorkspaceAccessProps) {
  const title = driverAccess ? 'Driver workspace' : identity ? 'Full workspace' : 'Public preview';
  const description = driverAccess
    ? identity
      ? 'Your account has driver access: congestion and assigned routes. An administrator can grant operator access to planning, ferry and analytics tools.'
      : 'You continued as a driver: congestion and assigned routes. Sign in with an authorized operator account for planning, ferry and analytics tools.'
    : 'Congestion, route planning and analytics are available here. Find ferry departures and port intelligence in Analytics → Ports.';
  const Icon = driverAccess ? CarFront : ShieldCheck;

  return (
    <section className="workspace-access" aria-label="Workspace access">
      <span className="workspace-access__icon" aria-hidden="true"><Icon size={ICON_SIZE.large} /></span>
      <div className="workspace-access__copy">
        <div className="workspace-access__heading">
          <strong>{title}</strong>
          <span>{identity ? `Account role: ${identity.role}` : driverAccess ? 'Guest session' : 'No account signed in'}</span>
        </div>
        <p>{description}</p>
      </div>
      {identity || signInAvailable ? (
        <button type="button" className="workspace-access__action" onClick={onOpenAccount} aria-haspopup="dialog">
          {identity ? <ShieldCheck size={ICON_SIZE.medium} aria-hidden="true" /> : <LogIn size={ICON_SIZE.medium} aria-hidden="true" />}
          {identity ? 'Account details' : driverAccess ? 'Operator sign-in' : 'Sign in'}
        </button>
      ) : <span className="workspace-access__unavailable">Operator sign-in unavailable</span>}
    </section>
  );
}
