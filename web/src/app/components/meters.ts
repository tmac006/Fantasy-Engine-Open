import { DecimalPipe } from '@angular/common';
import { Component, input } from '@angular/core';

import type { Outlook } from '../services/api';

/**
 * Risk and reward bars.
 *
 * The gradient sits on the full track and an overlay hides the unfilled part,
 * so a colour means the same thing at the same position on every card: a short
 * risk bar is entirely orange, and only a genuinely risky player reaches red.
 * Putting the gradient on the fill itself would tip even a 10% bar into red.
 *
 * The numbers are repeated as text because colour alone should not carry the
 * meaning -- and because "31% chance of a dud week" is more use than a shade.
 */
@Component({
  selector: 'app-meters',
  imports: [DecimalPipe],
  template: `
    <div class="meters">
      <div class="meter-row">
        <span class="meter-label">Risk</span>
        <span class="meter" [style.--pct.%]="outlook().risk">
          <span class="meter-track risk"></span>
          <span class="meter-mask"></span>
        </span>
        <span class="meter-value">{{ outlook().bust_probability * 100 | number: '1.0-0' }}% dud</span>
      </div>
      <div class="meter-row">
        <span class="meter-label">Reward</span>
        <span class="meter" [style.--pct.%]="outlook().reward">
          <span class="meter-track reward"></span>
          <span class="meter-mask"></span>
        </span>
        <span class="meter-value">{{ outlook().projected_ceiling | number: '1.0-1' }} ceiling</span>
      </div>
      <div class="meter-caption">
        <span class="outlook-label">{{ outlook().label }}</span>
        @if (showDrivers()) {
          @for (driver of outlook().drivers.slice(1); track driver) {
            <span class="sep">·</span>
            <span>{{ driver }}</span>
          }
        }
      </div>
    </div>
  `,
})
export class MetersComponent {
  readonly outlook = input.required<Outlook>();
  readonly showDrivers = input(true);
}
