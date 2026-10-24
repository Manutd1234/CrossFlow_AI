/* @vitest-environment jsdom */

import { act, type ReactElement } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import type { RouteLocation } from '../../types';
import { LocationSearch } from './LocationSearch';

let root: Root | undefined;

function renderIntoDom(element: ReactElement): HTMLElement {
  const container = document.createElement('div');
  document.body.append(container);
  root = createRoot(container);
  act(() => root?.render(element));
  return container;
}

function press(input: HTMLInputElement, key: string): void {
  act(() => {
    input.dispatchEvent(new KeyboardEvent('keydown', {
      key,
      bubbles: true,
      cancelable: true,
    }));
  });
}

afterEach(() => {
  if (root) act(() => root?.unmount());
  root = undefined;
  document.body.replaceChildren();
});

describe('LocationSearch keyboard navigation', () => {
  it('keeps the floating menu selectable and closes it on outside click or Tab', () => {
    const onChange = vi.fn();
    const place: RouteLocation = { id: 'port', name: 'Batam Centre', category: 'Terminal', lat: 1.13, lng: 104.05 };
    const container = renderIntoDom(<LocationSearch id="floating-place" label="From" value={null}
      onChange={onChange} namedLocations={[place]} compactLayout savedPlacesOnly />);
    const input = container.querySelector<HTMLInputElement>('input')!;
    act(() => input.focus());
    const menu = document.getElementById('floating-place-options')!;
    expect(menu.parentElement).toBe(document.body);
    expect(container.contains(menu)).toBe(false);
    const option = menu.querySelector<HTMLButtonElement>('[role="option"]')!;
    act(() => option.dispatchEvent(new MouseEvent('mousedown', { bubbles: true })));
    expect(input.getAttribute('aria-expanded')).toBe('true');
    act(() => option.click());
    expect(onChange).toHaveBeenCalledWith({ lat: place.lat, lng: place.lng, display_name: place.name });
    press(input, 'ArrowDown');
    act(() => document.body.dispatchEvent(new MouseEvent('mousedown', { bubbles: true })));
    expect(input.getAttribute('aria-expanded')).toBe('false');
    press(input, 'ArrowDown');
    press(input, 'Tab');
    expect(document.getElementById('floating-place-options')).toBeNull();
  });

  it('reopens matching suggestions when typing after the menu was dismissed', () => {
    const place: RouteLocation = { id: 'port', name: 'Batam Centre', category: 'Terminal', lat: 1.13, lng: 104.05 };
    const container = renderIntoDom(<LocationSearch id="edit-place" label="From" value={place}
      onChange={vi.fn()} namedLocations={[place]} />);
    const input = container.querySelector<HTMLInputElement>('input')!;
    act(() => input.focus());
    press(input, 'Escape');
    expect(input.getAttribute('aria-expanded')).toBe('false');
    act(() => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, 'Batam');
      input.dispatchEvent(new Event('input', { bubbles: true }));
    });
    expect(input.getAttribute('aria-expanded')).toBe('true');
    expect(document.getElementById('edit-place-options')?.textContent).toContain('Batam Centre');
  });

  it('scrolls keyboard selection within the menu but never scrolls on pointer hover', () => {
    const namedLocations: RouteLocation[] = [
      { id: 'first', name: 'First port', category: 'Terminal', lat: 1.13, lng: 104.05 },
      { id: 'second', name: 'Second port', category: 'Terminal', lat: 1.14, lng: 104.06 },
    ];
    const container = renderIntoDom(<LocationSearch id="scroll-place" label="From" value={null}
      onChange={vi.fn()} namedLocations={namedLocations} savedPlacesOnly />);
    const input = container.querySelector<HTMLInputElement>('input')!;
    act(() => input.focus());
    const menu = document.getElementById('scroll-place-options')!;
    const options = menu.querySelectorAll<HTMLButtonElement>('[role="option"]');
    menu.getBoundingClientRect = () => new DOMRect(0, 0, 240, 100);
    options[0].getBoundingClientRect = () => new DOMRect(0, 150, 240, 30);
    options[1].getBoundingClientRect = () => new DOMRect(0, 500, 240, 30);
    press(input, 'ArrowDown');
    expect(menu.scrollTop).toBe(80);
    act(() => options[1].dispatchEvent(new MouseEvent('mouseover', { bubbles: true })));
    expect(input.getAttribute('aria-activedescendant')).toBe(options[1].id);
    expect(menu.scrollTop).toBe(80);
    expect(container.scrollTop).toBe(0);
  });

  it('reuses the location menu for read-only saved-place selection', () => {
    const onNamedLocationSelect = vi.fn();
    const namedLocations: RouteLocation[] = [
      {
        id: 'origin',
        name: 'Batamindo Industrial Park',
        category: 'Industry',
        lat: 1.0605,
        lng: 104.0303,
      },
      {
        id: 'destination',
        name: 'Batam Centre Ferry Terminal',
        category: 'Ferry terminal',
        lat: 1.1318,
        lng: 104.0554,
      },
    ];
    const container = renderIntoDom(
      <LocationSearch
        id="saved-origin"
        label="From"
        value={namedLocations[0]}
        onChange={vi.fn()}
        onNamedLocationSelect={onNamedLocationSelect}
        namedLocations={namedLocations}
        compactLayout
        savedPlacesOnly
      />,
    );

    const input = container.querySelector<HTMLInputElement>('#saved-origin');
    expect(input?.readOnly).toBe(true);
    expect(input?.closest('.location-search-compact')).not.toBeNull();

    act(() => input?.focus());
    const menu = document.querySelector('#saved-origin-options');
    const options = Array.from(menu?.querySelectorAll<HTMLButtonElement>('[role="option"]') ?? []);
    expect(menu?.textContent).toContain('SAVED PLACES');
    expect(menu?.textContent).not.toContain('Use my current location');
    expect(options).toHaveLength(2);

    act(() => options[1]?.click());
    expect(onNamedLocationSelect).toHaveBeenCalledWith(namedLocations[1]);
  });

  it('can hide visible action buttons while keeping the map picker suggestion', () => {
    const container = renderIntoDom(
      <LocationSearch
        id="sidebar-location"
        label="From"
        value={null}
        onChange={vi.fn()}
        namedLocations={[]}
        onOpenMapPicker={vi.fn()}
        showMapPickerButton={false}
        showSearchButton={false}
      />,
    );

    const visibleButtonLabels = Array.from(container.querySelectorAll('button'))
      .map(button => button.textContent?.trim());
    expect(visibleButtonLabels).not.toContain('Pick on map');
    expect(visibleButtonLabels).not.toContain('Search');

    const input = container.querySelector<HTMLInputElement>('#sidebar-location');
    act(() => input?.focus());
    expect(document.querySelector('#sidebar-location-options-map-picker')).not.toBeNull();
  });

  it('tracks an active option, selects it with Enter, and resets it with Escape', () => {
    const onChange = vi.fn();
    const onOpenMapPicker = vi.fn();
    const namedLocations: RouteLocation[] = [{
      id: 'batam-centre',
      name: 'Batam Centre Ferry Terminal',
      category: 'Ferry terminal',
      lat: 1.1318,
      lng: 104.0554,
    }];
    const container = renderIntoDom(
      <LocationSearch
        id="origin-location"
        label="From"
        value={null}
        onChange={onChange}
        namedLocations={namedLocations}
        onOpenMapPicker={onOpenMapPicker}
      />,
    );
    const input = container.querySelector<HTMLInputElement>('#origin-location');
    const searchButton = container.querySelector<HTMLButtonElement>('[aria-label="Search Singapore or Batam places"]');
    expect(input).not.toBeNull();
    expect(searchButton?.classList.contains('ui-sand-interactive')).toBe(true);

    act(() => input?.focus());
    press(input!, 'ArrowUp');
    const wrappedOptionId = input?.getAttribute('aria-activedescendant');
    expect(wrappedOptionId).toBeTruthy();
    expect(document.getElementById(wrappedOptionId!)?.getAttribute('aria-selected')).toBe('true');

    press(input!, 'Escape');
    expect(input?.getAttribute('aria-expanded')).toBe('false');
    expect(input?.hasAttribute('aria-activedescendant')).toBe(false);

    press(input!, 'ArrowDown');
    expect(input?.getAttribute('aria-activedescendant'))
      .toBe('origin-location-options-map-picker');
    press(input!, 'ArrowDown');
    expect(input?.getAttribute('aria-activedescendant'))
      .toBe('origin-location-options-current-location');
    press(input!, 'ArrowDown');
    expect(input?.getAttribute('aria-activedescendant'))
      .toBe('origin-location-options-named-0');

    press(input!, 'Enter');
    expect(onChange).toHaveBeenCalledWith({
      lat: 1.1318,
      lng: 104.0554,
      display_name: 'Batam Centre Ferry Terminal',
    });
    expect(onOpenMapPicker).not.toHaveBeenCalled();
    expect(input?.getAttribute('aria-expanded')).toBe('false');
    expect(input?.hasAttribute('aria-activedescendant')).toBe(false);
  });
});
