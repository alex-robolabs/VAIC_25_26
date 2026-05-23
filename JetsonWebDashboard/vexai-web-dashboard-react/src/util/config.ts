import { Element } from "../lib/types";
import { images } from "./images";

/**
 * General configuration for the application
 */
export const config = {
  // Derive from the page's own host so the dashboard works whether viewed
  // from the Pi itself (localhost), a coach laptop on the robot hotspot
  // (10.42.0.1), or any other host on the dev LAN. Falls back to the
  // historical hotspot IP for non-browser contexts (SSR, tests).
  socketIP: typeof window !== "undefined" ? window.location.hostname : "10.42.0.1",
  socketPort: "3030",

  /**
   * Default image width captured by the camera on the robot
   */
  SCALE_X: 640,

  /**
   * Default image height captured by the camera on the robot
   */
  SCALE_Y: 480,

  /**
   * Rate at which the data services polls for data (ms)
   */
  pollingInterval: 60,
  logDataResponse: false,
  detectOutOfBoundsElements: true,
  colors: {
    red: "#D22630",
    darkRed: "#971c22",
    gray: "#939597",
    darkGray: "#58585B",
    darkerGray: "#111111",
    blue: "#0077C8",
    darkBlue: "#004d80",
    black: "#000000",
    white: "#F4F2FF",
    orange: "#fca503",
    darkOrange: "#bf7e04",
    purple: "#7466F1",
    darkPurple: "#433b87",
    grayPurple: "#293045",
  },
  field: {
    dimension: 3.6576, // meters
    oov: 0.1,
    texture: images.field,
    scale: 1.5,
    compass: {
      texture: images.map.compass,
      scale: 0.23,
      ringPositionCorrectionX: 0.1,
      lineScale: 0.03,
      numberOffset: 0.045,
      numberScale: 0.017,
    },
    fog: {
      opacity: 0.75,
    },
    ruler: {
      texture: images.map.ruler,
      xySidebarOffset: 0.26,
      xySidebarMarkersOffset: 0.22,
      xySidebarArrowOffset: 0.22,
      xySidebarSizeMultiplier: 0.025,
      arrowScale: 0.1,
    },
    robot: {
      length: 0.4191, // meters
      width: 0.3175, // meters
      scale: 1.5,
      texture: images.robot,
      textureWidth: 1024, // pixels
      textureHeight: 1024, // pixels
      fov: 50,
    },
  },
  elements: {
    textures: {
      [Element.BallBlue]: images.elements.blueBall,
      [Element.BallRed]: images.elements.redBall,
    },
    size: {
      [Element.BallBlue]: {
        height: 0.08255,
        width: 0.08255,
        scale: 2.0,
      },
      [Element.BallRed]: {
        height: 0.08255,
        width: 0.08255,
        scale: 2.0
      },
    },
    borderColors: {
      [Element.BallRed]: "rgba(255, 0, 0, .8)",
      [Element.BallBlue]: "rgba(0, 0, 255, .8)",
    },
    backgroundColors: {
      [Element.BallRed]: "rgba(255, 0, 0, .3)",
      [Element.BallBlue]: "rgba(0, 0, 255, .3)",
    },
    label: {
      textColors: {
        white: "rgba(255, 255, 255, 1)",
        black: "rgba(0, 0, 0, 1)",
      },
      text: {
        [Element.BallRed]: "Red Ball",
        [Element.BallBlue]: "Blue Ball",
      },
    },
  },
};
