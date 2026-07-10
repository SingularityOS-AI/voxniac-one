---
name: Technical Warmth Console
colors:
  surface: '#fff8f6'
  surface-dim: '#ecd5d0'
  surface-bright: '#fff8f6'
  surface-container-lowest: '#ffffff'
  surface-container-low: '#fff1ed'
  surface-container: '#ffe9e4'
  surface-container-high: '#fae3de'
  surface-container-highest: '#f4ded8'
  on-surface: '#241916'
  on-surface-variant: '#58423c'
  inverse-surface: '#3b2d2a'
  inverse-on-surface: '#ffede8'
  outline: '#8b716a'
  outline-variant: '#dfc0b8'
  surface-tint: '#a73919'
  primary: '#a63818'
  on-primary: '#ffffff'
  primary-container: '#c7502e'
  on-primary-container: '#ffffff'
  inverse-primary: '#ffb5a0'
  secondary: '#665c58'
  on-secondary: '#ffffff'
  secondary-container: '#eaddd7'
  on-secondary-container: '#6a615c'
  tertiary: '#00657f'
  on-tertiary: '#ffffff'
  tertiary-container: '#0080a0'
  on-tertiary-container: '#fffeff'
  error: '#ba1a1a'
  on-error: '#ffffff'
  error-container: '#ffdad6'
  on-error-container: '#93000a'
  primary-fixed: '#ffdbd1'
  primary-fixed-dim: '#ffb5a0'
  on-primary-fixed: '#3b0900'
  on-primary-fixed-variant: '#862202'
  secondary-fixed: '#ede0d9'
  secondary-fixed-dim: '#d1c4be'
  on-secondary-fixed: '#211a16'
  on-secondary-fixed-variant: '#4e4540'
  tertiary-fixed: '#baeaff'
  tertiary-fixed-dim: '#77d2f5'
  on-tertiary-fixed: '#001f29'
  on-tertiary-fixed-variant: '#004d62'
  background: '#fff8f6'
  on-background: '#241916'
  surface-variant: '#f4ded8'
typography:
  display:
    fontFamily: Space Grotesk
    fontSize: 48px
    fontWeight: '700'
    lineHeight: '1.1'
    letterSpacing: -0.02em
  headline-lg:
    fontFamily: Space Grotesk
    fontSize: 32px
    fontWeight: '600'
    lineHeight: '1.2'
  headline-lg-mobile:
    fontFamily: Space Grotesk
    fontSize: 24px
    fontWeight: '600'
    lineHeight: '1.2'
  headline-md:
    fontFamily: Space Grotesk
    fontSize: 24px
    fontWeight: '500'
    lineHeight: '1.3'
  body-lg:
    fontFamily: Inter
    fontSize: 18px
    fontWeight: '400'
    lineHeight: '1.6'
  body-md:
    fontFamily: Inter
    fontSize: 16px
    fontWeight: '400'
    lineHeight: '1.5'
  label-mono:
    fontFamily: JetBrains Mono
    fontSize: 14px
    fontWeight: '500'
    lineHeight: '1.0'
    letterSpacing: 0.05em
  metric-xl:
    fontFamily: JetBrains Mono
    fontSize: 40px
    fontWeight: '700'
    lineHeight: '1.0'
rounded:
  sm: 0.25rem
  DEFAULT: 0.5rem
  md: 0.75rem
  lg: 1rem
  xl: 1.5rem
  full: 9999px
spacing:
  base: 8px
  xs: 4px
  sm: 12px
  md: 24px
  lg: 48px
  xl: 64px
  gutter: 24px
  margin: 32px
---

## Brand & Style
The design system is engineered for a high-stakes AI voice sales environment, balancing the cold precision of a technical operations room with a warm, human-centric editorial feel. The brand personality is authoritative yet inviting—designed to make complex automation feel reliable and under control.

The style is **Technical Minimalism**. It utilizes a structured, grid-based layout inspired by modern command centers and industrial design. It avoids decorative elements in favor of functional aesthetics: thin lines, monospaced data points, and clear hierarchical boundaries. The interface should feel like a high-end physical hardware console translated into a digital space, using high-contrast ink on a soft, warm paper-like background.

## Colors
The palette is centered on a "Warm Cream" background (#FAF6EF) to reduce eye strain during long monitoring sessions, departing from the harsh whites of standard SaaS. 

- **Primary (Burnt Orange):** Used sparingly for calls to action, active states, and critical highlights. It provides a sharp, energetic contrast to the neutral base.
- **Ink (Dark Charcoal):** The primary text color. It is deep and authoritative, ensuring maximum legibility against the cream and white surfaces.
- **Success (Kernel Green):** A grounded, earthy green used specifically for system status indicators ("ONLINE") and positive growth metrics.
- **Neutral/Borders:** A desaturated version of the charcoal at low opacities (10-15%) is used for structural lines and connectors.

## Typography
Typography is the primary driver of the "control room" aesthetic. 

- **Space Grotesk** handles the structural hierarchy. Its geometric, slightly quirky terminals provide a futuristic, technical character for headers and section titles.
- **Inter** is used for all long-form content and UI labels where clarity is paramount. It is the workhorse font that ensures the system remains approachable.
- **JetBrains Mono** is reserved for status codes, timestamps, metrics, and technical labels. This monospaced font reinforces the "precise" nature of the AI agent, making data look curated and systematic.

All headers should utilize a tighter letter spacing to maintain a "locked-in" technical feel, while mono labels should have increased tracking for better scanability.

## Layout & Spacing
The layout follows a **Fixed Grid** philosophy for the main control dashboard to ensure that technical data points remain in predictable locations. A 12-column grid is used for desktop with a generous 32px outer margin.

A key feature of the layout is the **Pipeline Connector**. Use 1px vertical or horizontal lines (20% opacity Charcoal) to visually link sequential steps in the AI sales process. 

### Breakpoints
- **Desktop (1280px+):** Full dashboard with persistent sidebar.
- **Tablet (768px - 1279px):** Content scales to 8 columns; sidebar collapses to icons.
- **Mobile (<768px):** Single column flow. Spacing (md) reduces from 24px to 16px to maximize screen real estate.

## Elevation & Depth
This design system rejects heavy shadows in favor of **Tonal Layers** and **Low-Contrast Outlines**.

- **Level 0 (Background):** The Warm Cream (#FAF6EF) base.
- **Level 1 (Cards):** Pure White (#FFFFFF) surfaces with a 1px solid border (#2B2420 at 10% opacity). This creates a "sheet" effect that feels tactile but flat.
- **Level 2 (Dropdowns/Modals):** Pure White with a slightly darker border and a very subtle, sharp ambient shadow (0px 4px 12px rgba(43, 36, 32, 0.08)).

Depth is primarily signaled through the "stacking" of containers and the use of the Burnt Orange accent to pull the user's eye to the most critical interactive layer.

## Shapes
The shape language is "Soft-Technical." While a purely brutalist console would use sharp corners, this system uses 0.5rem (8px) rounding to introduce the "warmth" required for a premium user experience.

- **Standard Elements:** 0.5rem (8px) radius for cards and input fields.
- **Large Components:** 1rem (16px) for major dashboard sections.
- **Buttons:** 0.5rem (8px) for a sturdy, industrial feel. Avoid pill shapes as they appear too casual for a control room environment.

## Components
### Buttons
- **Primary:** Solid Burnt Orange with White text. No gradients. Sharp hover state (slightly darker orange).
- **Secondary:** Transparent with a 1px Charcoal border.
- **Tertiary:** Text-only JetBrains Mono with a bottom underline that appears on hover.

### Cards & Containers
Cards must use white backgrounds to pop against the cream canvas. Each card should have a clear header section separated by a 1px horizontal line. Use JetBrains Mono for the card title in the top-left corner.

### Inputs & Fields
Inputs should have a subtle beige fill (5% opacity Charcoal) and a 1px border. Focus states are indicated by the border changing to Burnt Orange. Use JetBrains Mono for the input text to emphasize the "data entry" aspect.

### Status Indicators
The "KERNEL: ONLINE" status should be a small, solid green circle next to the label. For a "live" feel, a subtle 2px pulse animation can be applied to the success indicator.

### Pipeline Steps
Vertical connectors (1px lines) should flow between cards to represent the customer journey. Circles on the line indicate "nodes" where the AI agent is performing an action.