import React, { useState, useEffect, useRef } from 'react';
import axios from 'axios';
import YouTube from 'react-youtube';
import './App.css';

function App() {
  const [url, setUrl] = useState('');
  const [translated_transcript, setTranslatedTranscript] = useState([]);
  const [transcript, setTranscript] = useState([]);
  const [videoId, setVideoId] = useState('');
  const [player, setPlayer] = useState(null);
  
  // TRACKING STATE
  const [currentLineIndex, setCurrentLineIndex] = useState(0);
  const [userInput, setUserInput] = useState('');
  const [showInput, setShowInput] = useState(false);  
  const [fromLang, setFromLang] = useState('es'); // Default: Spanish
  const [toLang, setToLang] = useState('zh-CN');   // Default: English

  const inputRef = useRef(null);
  const languages = [
    { code: 'es', name: 'Spanish', flag: '🇪🇸' },
    { code: 'en', name: 'English', flag: '🇺🇸' },
    { code: 'fr', name: 'French', flag: '🇫🇷' },
    { code: 'de', name: 'German', flag: '🇩🇪' },
  ];

  const handleSubmit = async (e) => {
    e.preventDefault();
    try {
      const response = await axios.post('http://127.0.0.1:5000/api/transcript', { 
        url,
        from_lang: fromLang,
        to_lang: toLang
       });
      setTranscript(response.data.snippets); // Note: Make sure your backend sends 'snippets'
      setTranslatedTranscript(response.data.translated_snippets);
      setVideoId(response.data.video_id);
      setCurrentLineIndex(0); // Reset to start
      setShowInput(false);
    } catch (error) {
      console.error("Error:", error);
    }
  };

  // ---------------------------------------------------------
  // THE BRAKE PEDAL (Auto-Pause Logic)
  // ---------------------------------------------------------
  useEffect(() => {
    let interval;
    if (player && transcript.length > 0) {
      interval = setInterval(async () => {
        // We only check time if we are NOT already waiting for user input
        if (!showInput) {
          const currentTime = await player.getCurrentTime();
          const currentLine = transcript[currentLineIndex];
          const endTime = currentLineIndex < transcript.length - 1 && currentLine.start + currentLine.duration > transcript[currentLineIndex + 1].start ? transcript[currentLineIndex + 1].start-.2 : currentLine.start + currentLine.duration;
            // If we reached the end of the line...
          if (currentTime >= endTime) {
            player.pauseVideo();   // 1. Pause Video
            setShowInput(true);    // 2. Show Input Box
          }
        }
      }, 100); // Check faster (every 0.1s) for better precision
    }
    return () => clearInterval(interval);
  }, [player, transcript, currentLineIndex, showInput]);


  // ---------------------------------------------------------
  // THE GAS PEDAL (Go to next line)
  // ---------------------------------------------------------
  const handleInputSubmit = (e) => {
    if (e.key === 'Enter') {
      // Move to next line if available
      if (currentLineIndex < transcript.length - 1) {
        const nextIndex = currentLineIndex + 1;
        setCurrentLineIndex(nextIndex);
        setUserInput('');       // Clear text
        setShowInput(false);    // Hide box
        
        player.playVideo();     // Resume Video
      } else {
        alert("You finished the video!");
      }
    }
  };

  // Auto-focus the input box when it appears
  useEffect(() => {
    if (showInput && inputRef.current) {
      inputRef.current.focus();
    }
  }, [showInput]);


  // ---------------------------------------------------------
  // RENDER
  // ---------------------------------------------------------
  return (
    <div className="App">
      <header className="App-header">
        <h1>Vidioma</h1>
        
        {/* Search Bar */}
        {!videoId && (
          <form onSubmit={handleSubmit} className="search-box">
            <input 
              type="text" 
              placeholder="Paste YouTube URL..." 
              value={url}
              onChange={(e) => setUrl(e.target.value)}
            />
            <button type="submit">Go</button>
          </form>
        )}

        <div className="content-area">
          {/* Video Player */}
          {videoId && (
            <div className="video-section">
              <div className="video-wrapper">
                <YouTube 
                  videoId={videoId} 
                  opts={{ 
                    height: '390', 
                    width: '640',
                    playerVars: {
                      rel: 0, // 1. Limit suggestions to same channel (Best we can do via API)
                      modestbranding: 1, // Remove logos
                      autoplay: 1, // Auto-play on load
                    }
                  }}
                  onReady={(event) => setPlayer(event.target)}
                />
                
                {/* 2. The Privacy Curtain - Only shows when paused for input */}
                {showInput && <div className="video-curtain" />}
              </div>
            </div>
          )}

          {/* The New "Focus Mode" Display */}
          {transcript.length > 0 && (
            <div className="focus-card">
              
              <div className="language-indicator">
                {/* From Language Dropdown */}
                <select 
                  value={fromLang} 
                  onChange={(e) => setFromLang(e.target.value)}
                  className="lang-dropdown"
                >
                  {languages.map((lang) => (
                    <option key={lang.code} value={lang.code}>
                      {lang.flag} {lang.name}
                    </option>
                  ))}
                </select>

                <span className="arrow">➔</span>

                {/* To Language Dropdown */}
                <select 
                  value={toLang} 
                  onChange={(e) => setToLang(e.target.value)}
                  className="lang-dropdown"
                >
                  {languages.map((lang) => (
                    <option key={lang.code} value={lang.code}>
                      {lang.flag} {lang.name}
                    </option>
                  ))}
                </select>
              </div>
              {/* 1. Show the Current Sentence */}
              <h2 className="current-text">
                {transcript[currentLineIndex].source}
              </h2>
              {/* 1.5. Show the Translated Sentence */}
              <h2 className="current-text">
                {translated_transcript[currentLineIndex]}
              </h2>

              {/* 2. Show Input ONLY when paused */}
              {showInput ? (
                <div className="input-container">
                  <input 
                    ref={inputRef}
                    type="text" 
                    className="big-input"
                    placeholder="Type translation..." 
                    value={userInput}
                    onChange={(e) => setUserInput(e.target.value)}
                    onKeyDown={handleInputSubmit}
                  />
                  <p className="hint">Press Enter to continue</p>
                </div>
              ) : (
                <p className="listening-indicator">👂 Listening...</p>
              )}

              <div className="progress">
                Line {currentLineIndex + 1} of {transcript.length}
              </div>
            </div>
          )}
        </div>
      </header>
    </div>
  );
}

export default App;