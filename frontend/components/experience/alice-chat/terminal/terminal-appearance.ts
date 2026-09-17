import type { ITheme } from '@xterm/xterm'

const ANSI: Required<Pick<ITheme,
  'black' | 'red' | 'green' | 'yellow' | 'blue' | 'magenta' | 'cyan' | 'white'
  | 'brightBlack' | 'brightRed' | 'brightGreen' | 'brightYellow'
  | 'brightBlue' | 'brightMagenta' | 'brightCyan' | 'brightWhite'>> = {
  black: '#2e3436', red: '#cc0000', green: '#4e9a06', yellow: '#c4a000',
  blue: '#3465a4', magenta: '#75507b', cyan: '#06989a', white: '#d3d7cf',
  brightBlack: '#555753', brightRed: '#ef2929', brightGreen: '#8ae234', brightYellow: '#fce94f',
  brightBlue: '#729fcf', brightMagenta: '#ad7fa8', brightCyan: '#34e2e2', brightWhite: '#eeeeec',
}

function cssToken(style: CSSStyleDeclaration, name: string, fallback: string): string {
  return style.getPropertyValue(name).trim() || fallback
}

export function resolveTerminalTheme(): ITheme {
  const style = getComputedStyle(document.documentElement)
  return {
    ...ANSI,
    background: cssToken(style, '--background', '#0b0d10'),
    foreground: cssToken(style, '--foreground', '#f4f4f5'),
    cursor: cssToken(style, '--foreground', '#f4f4f5'),
    cursorAccent: cssToken(style, '--background', '#0b0d10'),
    selectionBackground: cssToken(style, '--accent', '#334155'),
  }
}

export function terminalThemesEqual(left: ITheme | undefined, right: ITheme): boolean {
  if (!left) return false
  const keys = new Set([...Object.keys(left), ...Object.keys(right)])
  return [...keys].every((key) => left[key as keyof ITheme] === right[key as keyof ITheme])
}
