using _3Brain.BioCamDriver;
using _3Brain.Common;
using _3Brain.Processing.Core;
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Data;
using System.Diagnostics;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Runtime;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;
using static System.Net.Mime.MediaTypeNames;

namespace _3Brain.BioCamCL
{
    public partial class MainForm : Form
    {
        #region Constants
        const int WAIT_TIME_BEFORE_PROCESSING_MS = 2000;
        #endregion

        #region Fields
        int measuringPeriodMs;
        int processingDurationSec;
        int acquisitionTimePeriodMs;
        int nEndPoints;
        BioCamDupleX bioCam;
        int bioCamSlotIndex;
        bool isStreaming;
        bool isProcessing;
        bool isCollectingData;
        StimEndPoint[] positiveEndPoints;
        StimEndPoint[] negativeEndPoints;
        Thread processingThread;
        AutoResetEvent processingWaitHandle;
        Stopwatch stopwatch;
        DateTime streamingStartDateTime;
        TimeSpan processingStartTimeSpan;
        DateTime lastMeasureTakenDateTime;
        long acquisitionTimePeriodLastElapsedTicks;
        List<double> closedLoopLatencyMsList;
        List<double> acquisitionTimePeriodMsList;
        ulong timeStamp;
        bool reportCll;
        #endregion

        #region Constructor

        public MainForm()
        {
            InitializeComponent();

            // give the app process the highest possible priority
            using (Process process = Process.GetCurrentProcess())
            {
                process.PriorityClass = ProcessPriorityClass.RealTime;
            }

            // initialize fields
            reportSelectionComboBox.SelectedIndex = 1;
            logListBox.Items.Clear();
            bioCamSlotIndex = -1;
            isStreaming = false;

            // activate the BioCamPool and start listening to BioCAMs status changed (connecting or disconnecting)        
            BioCamPool.Activate();
            BioCamPool.BioCamsStatusChanged += BioCamPool_BioCamsStatusChanged;
        }
        #endregion

        #region Methods

        #region Form Overridden Methods
        protected override void OnFormClosed(FormClosedEventArgs e)
        {
            base.OnFormClosed(e);

            // deactivate the BioCamPool
            BioCamPool.Deactivate();
        }
        #endregion

        #region BioCAM Controls Methods 
        private void TakeBioCamControl()
        {
            if (bioCam is null)
            {
                ReportLog("Taking BioCAM control ...");

                // finds a free BioCAM slot index
                while (bioCamSlotIndex < 0)
                {
                    int[] free = BioCamPool.GetSlotIndexesFreeBioCam();

                    if (free.Length > 0)
                        bioCamSlotIndex = free[0];
                    else
                        Thread.Sleep(500);
                }

                // takes a free BioCAM from the pool
                bioCam = BioCamPool.TakeBioCamControl(bioCamSlotIndex) as BioCamDupleX;

                // initialize the BioCAM stimulator module
                bioCam?.Stimulator?.Initialize();
            }
        }
        private void ReleaseBioCamControl()
        {
            if (bioCam is null)
                return;

            ReportLog("Releasing BioCAM control ...");

            // close the BioCAM stimulator module
            bioCam.Stimulator.Close();

            // release the bioCAM back to the pool
            BioCamPool.ReleaseBioCamControl(bioCamSlotIndex);
            bioCamSlotIndex = -1;
            bioCam = null;
        }

        private bool StartAcquisition()
        {
            if (bioCam is null)
                return false;

            ReportLog("Starting BioCAM acquisition.");

            if (!bioCam.IsConnected)
                throw new Exception("Connection Problem: The BioCAM does not appear to be connected.");
            if (!bioCam.MeaPlate.IsConnected)
                throw new Exception("Connection Problem: The Plate does not appear to be connected.");

            // subscribe to BioCAMs events
            bioCam.DataReceived -= BioCam_DataReceived;
            bioCam.DataReceived += BioCam_DataReceived;
            bioCam.DataStreamingError -= BioCam_DataStreamingError;
            bioCam.DataStreamingError += BioCam_DataStreamingError;
            bioCam.DataLossAsync -= BioCam_DataLossAsync;
            bioCam.DataLossAsync += BioCam_DataLossAsync;
            bioCam.MeaPlate.ChipLanderStatusChanged -= MeaPlate_ChipLanderStatusChanged;
            bioCam.MeaPlate.ChipLanderStatusChanged += MeaPlate_ChipLanderStatusChanged;

            // initialize and/or reset processing fields           
            acquisitionTimePeriodLastElapsedTicks = 0;
            measuringPeriodMs = Convert.ToInt32(cllMeasuringPeriodMsNumericUpDown.Value);
            processingDurationSec = Convert.ToInt32(processingDurationMinNumericUpDown.Value * 60);
            acquisitionTimePeriodMs = Convert.ToInt32(acquisitionTimePeriodMsNumericUpDown.Value);
            nEndPoints = Convert.ToInt32(cllNumEndPointsNumericUpDown.Value);

            streamingStartDateTime = DateTime.MinValue;
            processingStartTimeSpan = TimeSpan.Zero;
            lastMeasureTakenDateTime = DateTime.Now;
            stopwatch = Stopwatch.StartNew();
            processingWaitHandle = new AutoResetEvent(false);
            var dataCapacity = ((processingDurationSec + 30) * 1000) / acquisitionTimePeriodMs;
            closedLoopLatencyMsList = new List<double>(dataCapacity);
            acquisitionTimePeriodMsList = new List<double>(dataCapacity);
            isCollectingData = true;

            // config the chip-lander
            if(bioCam.MeaPlate.IsChipLanderConnected)
            {
                bioCam.MeaPlate.SetChipLanderWell(Convert.ToInt32(clWellIdxNumericUpDown.Value));
            }

            // build stimulation end points
            BuildStimEndPoints();

            // initialize and start the processing thread
            isProcessing = true;
            processingThread = new Thread(ProcessingThreadBody);
            processingThread.Name = "BioCAM Processing Thread";
            processingThread.Priority = ThreadPriority.Highest;
            processingThread.Start();

            // start the BioCAM data acquisition
            isStreaming = bioCam.StartDataStreaming(dataPacketTimeSpanMs: acquisitionTimePeriodMs, optimizeDataPacketLatency: true);

            if (!isStreaming)
                throw new Exception("BioCAM Stream Problem: An error occurred while starting the acquisition.");

            // start the BioCAM stimulation module
            bioCam.Stimulator.Start();

            return true;
        }
        private void StopAcquisition()
        {
            if (processingThread is null)
                return;

            ReportLog("Stopping BioCAM acquisition.");

            // terminate the processing thread
            isProcessing = false;
            processingWaitHandle.Set();
            processingThread.Join();
            processingThread = null;

            // stop the BioCAM stimulation module
            bioCam.Stimulator.Stop();

            // stop the BioCAM data acquisition
            if (!bioCam.StopDataStreaming())
                throw new Exception("BioCAM Stream Problem: An error occurred while stopping the acquisition.");

            isStreaming = false;

            // unsubscribe to BioCAMs events
            bioCam.DataReceived -= BioCam_DataReceived;
            bioCam.DataStreamingError -= BioCam_DataStreamingError;
            bioCam.DataLossAsync -= BioCam_DataLossAsync;
            bioCam.MeaPlate.ChipLanderStatusChanged -= MeaPlate_ChipLanderStatusChanged;

            // analyze the processed data and report the results
            ReportProcessingResults();

            // dispose processing collections
            closedLoopLatencyMsList = null;
            acquisitionTimePeriodMsList = null;
            positiveEndPoints = null;
            negativeEndPoints = null;

            // enable all settings controls
            SetSettingControlsEnablingStatus(true);
        }
        private void TerminateAcquisition()
        {
            if (bioCam is null)
                return;

            try
            {
                if (isStreaming)
                {
                    StopAcquisition();
                }

                ReleaseBioCamControl();
            }
            catch (Exception ex)
            {
                ReportLog(ex.Message);
            }
        }
        #endregion

        #region Processing Methods
        private void ProcessingThreadBody()
        {
            while (isProcessing)
            {
                processingWaitHandle.WaitOne();

                if (!isProcessing)
                    return;

                if (reportCll)
                {
                    ulong closedLoopLatencyClockCycles;
                    GetClosedLoopLatency(timeStamp, out closedLoopLatencyClockCycles);

                    var closedLoopLatencyMs = bioCam.ClockCyclesToMilliseconds(closedLoopLatencyClockCycles);
                    closedLoopLatencyMsList.Add(closedLoopLatencyMs);
                }

                processingStartTimeSpan += TimeSpan.FromMilliseconds(measuringPeriodMs);
                ReportTime(processingStartTimeSpan);
            }
        }
        private void ReportProcessingResults()
        {
            if (InvokeRequired)
            {
                BeginInvoke((MethodInvoker)(() => ReportProcessingResults()));
                return;
            }

            if (acquisitionTimePeriodMsList.IsNullOrEmpty())
                return;

            // uniform collections count
            var requiredCount = (processingDurationSec * 1000) / acquisitionTimePeriodMs;

            if (closedLoopLatencyMsList.Count > requiredCount)
            {
                closedLoopLatencyMsList.RemoveRange(requiredCount, closedLoopLatencyMsList.Count - requiredCount);
            }

            if (closedLoopLatencyMsList.Count != acquisitionTimePeriodMsList.Count)
            {
                acquisitionTimePeriodMsList.RemoveRange(closedLoopLatencyMsList.Count, acquisitionTimePeriodMsList.Count - closedLoopLatencyMsList.Count);
            }

            // compute acquisition time statistics
            var acquisitionTimeValues = acquisitionTimePeriodMsList.ToArray();

            double acquisitionTimeMinMs;
            double acquisitionTimeMaxMs;
            StatisticalFunctions.MinMax(acquisitionTimeValues, out acquisitionTimeMinMs, out acquisitionTimeMaxMs);

            double acquisitionTimeMeanMs;
            double acquisitionTimeStdDevMs;
            StatisticalFunctions.MeanStdDev(acquisitionTimeValues, out acquisitionTimeMeanMs, out acquisitionTimeStdDevMs);

            // compute closed loop statistics
            var closedLoopTimeValues = closedLoopLatencyMsList.Where(x => !double.IsNaN(x)).ToArray();

            double closedLoopTimeMinMs;
            double closedLoopTimeMaxMs;
            double closedLoopTimeMeanMs;
            double closedLoopTimeStdDevMs;

            if (closedLoopTimeValues.Length < 2)
            {
                closedLoopTimeMinMs = closedLoopTimeMaxMs = closedLoopTimeMeanMs = closedLoopTimeStdDevMs = double.NaN;
            }
            else
            {
                StatisticalFunctions.MinMax(closedLoopTimeValues, out closedLoopTimeMinMs, out closedLoopTimeMaxMs);
                StatisticalFunctions.MeanStdDev(closedLoopTimeValues, out closedLoopTimeMeanMs, out closedLoopTimeStdDevMs);
            }

            // print the results
            ReportLog(string.Empty);
            ReportLog($"--- RESULTS --- ");
            ReportLog($"Data Packet Time (ms): {bioCam.DataFormat.DataPacketTimeSpanMs}");
            ReportLog($"Closed Loop Time (ms): Mean (±SD): {closedLoopTimeMeanMs:N2} (±{closedLoopTimeStdDevMs:N2}); Min: {closedLoopTimeMinMs:N2}; Max: {closedLoopTimeMaxMs:N2}");
            ReportLog($"Acquisition Time Period (ms): Mean (±SD): {acquisitionTimeMeanMs:N2} (±{acquisitionTimeStdDevMs:N2}); Min: {acquisitionTimeMinMs:N2}; Max: {acquisitionTimeMaxMs:N2}");
            ReportLog($"--------------- ");
            ReportLog(string.Empty);

            // save results to a csv file
            var stringBuilder = new StringBuilder();

            foreach (var item in closedLoopLatencyMsList)
            {
                stringBuilder.Append(item.ToString("F4", CultureInfo.CreateSpecificCulture("en-US")) + ", ");
            }

            stringBuilder.AppendLine();

            foreach (var item in acquisitionTimePeriodMsList)
            {
                stringBuilder.Append(item.ToString("F4", CultureInfo.CreateSpecificCulture("en-US")) + ", ");
            }

            stringBuilder.AppendLine();
            stringBuilder.Append("Processing Period (mm:ss:ff), ");
            stringBuilder.Append(processingStartTimeSpan.ToShortString(2) + ", ");
            stringBuilder.AppendLine();
            stringBuilder.Append("Measuring Period (ms), ");
            stringBuilder.Append(measuringPeriodMs + ", ");
            stringBuilder.AppendLine();
            stringBuilder.Append("Acquisition Time Period (ms), ");
            stringBuilder.Append(acquisitionTimePeriodMs + ", ");
            stringBuilder.AppendLine();
            stringBuilder.Append("# of Total EndPoints, ");
            stringBuilder.Append(nEndPoints);

            if (reportCll)
                File.WriteAllText($"./ClosedLoopLatencyTests__pd={processingStartTimeSpan.ToString("mm") + "min-" + processingStartTimeSpan.ToString("ss") + "s"}_mp={measuringPeriodMs}ms_atp={acquisitionTimePeriodMs}ms_neps={nEndPoints}.csv", stringBuilder.ToString());
            else
                File.WriteAllText($"./ClosedLoopLatencyTests__pd={processingStartTimeSpan.ToString("mm") + "min-" + processingStartTimeSpan.ToString("ss") + "s"}_mp={measuringPeriodMs}ms_atp={acquisitionTimePeriodMs}.csv", stringBuilder.ToString());
        }
        #endregion

        #region Utility Methods
        private void GetClosedLoopLatency(ulong timeStamp, out ulong closedLoopLatencyClockCycles)
        {
            ulong stimLatency;

            // instruct the BioCAM to release, as soon as possible, a rectangular biphasic stimulus between the positive and electrode endpoints
            bioCam.Stimulator.Send(RectangularStimPulse.Default, positiveEndPoints, negativeEndPoints, out stimLatency);

            closedLoopLatencyClockCycles = stimLatency - timeStamp;
        }
        private void BuildStimEndPoints()
        {
            var nPos = nEndPoints / 2;
            var nNeg = nEndPoints - nPos;

            // create the positive endpoints, the electrodes that represent the positive pole of the bipolar stimulation, each electrode being defined by a (row, col) coordinate (ChCoord)
            int r, c;
            positiveEndPoints = new StimEndPoint[nPos];
            for (int i = 0; i < nPos; i++)
            {
                r = Math.DivRem(i, 32, out c) + 1;
                c += 1;

                positiveEndPoints[i] = bioCam.Stimulator.GetInternalEndPoint(new ChCoord(r, c));
            }

            // create the negative endpoints, the electrodes that represent the negative pole of the bipolar stimulation
            negativeEndPoints = new StimEndPoint[nPos];
            for (int i = 0; i < nNeg; i++)
            {
                r = Math.DivRem(i, 32, out c) + 1;
                c += 33;

                negativeEndPoints[i] = bioCam.Stimulator.GetInternalEndPoint(new ChCoord(r, c));
            }
        }
        private double StopwatchTicksToMs(long ticks)
        {
            return ticks / (Stopwatch.Frequency / 1000D);
        }
        private void ReportLog(params string[] text)
        {
            if (InvokeRequired)
            {
                BeginInvoke((MethodInvoker)(() => ReportLog(text)));
                return;
            }

            if (!String.IsNullOrEmpty(String.Concat(text)))
                logListBox.Items.Add(String.Concat(text));
        }
        private void ReportTime(TimeSpan timeSpan)
        {
            if (InvokeRequired)
            {
                BeginInvoke((MethodInvoker)(() => ReportTime(timeSpan)));
                return;
            }

            timeLabelControl.Text = timeSpan.ToShortString(2);
        }
        private void SetSettingControlsEnablingStatus(bool enabled)
        {
            if (InvokeRequired)
            {
                BeginInvoke((MethodInvoker)(() => SetSettingControlsEnablingStatus(enabled)));
                return;
            }

            startButton.Enabled = enabled;
            reportSelectionComboBox.Enabled = enabled;
            processingDurationMinNumericUpDown.Enabled = enabled;
            cllMeasuringPeriodMsNumericUpDown.Enabled = enabled;
            acquisitionTimePeriodMsNumericUpDown.Enabled = enabled;
            cllNumEndPointsNumericUpDown.Enabled = enabled;
        }
        #endregion

        #endregion

        #region Events Handlers

        #region BioCAM Events Handlers
        private void BioCamPool_BioCamsStatusChanged(object sender, EventArgs e)
        {
            if (InvokeRequired)
            {
                BeginInvoke((MethodInvoker)(() => BioCamPool_BioCamsStatusChanged(sender, e)));
                return;
            }

            var slotInfoList = BioCamPool.BioCamSlotInfo.Where(x => x.IsBioCamConnected).ToList();

            if (slotInfoList.Count > 0)
            {
                var slotInfo = slotInfoList.First();

                var message = $"{slotInfo.CommercialModel} ({slotInfo.SerialNumberAndVersion}) is CONNECTED {(slotInfo.IsMeaPlateConnected ? $"and Plate [Model:{slotInfo.ConnectedMeaPlateModel.Value} | SN:{slotInfo.ConnectedMeaPlateSerial.Value}] is READY" : "(Plate is not)")}";

                ReportLog(message);

                clGroupBox.Enabled = slotInfo.IsMeaPlateConnected && slotInfo.ConnectedMeaPlateModel.Value.IsChipLander();
            }
        }
        private void BioCam_DataReceived(object sender, DataPacketReceivedEventArgs e)
        {
            if (!isCollectingData)
                return;

            if (streamingStartDateTime == DateTime.MinValue)
            {
                streamingStartDateTime = DateTime.Now;
                return;
            }

            var now = DateTime.Now;
            if (now.Subtract(streamingStartDateTime).TotalMilliseconds < WAIT_TIME_BEFORE_PROCESSING_MS)
            {
                acquisitionTimePeriodLastElapsedTicks = stopwatch.ElapsedTicks;
                return;
            }
            else if (acquisitionTimePeriodMsList.Count == 0)
            {
                ReportLog("Processing ...");
            }

            acquisitionTimePeriodMsList.Add(StopwatchTicksToMs(stopwatch.ElapsedTicks - acquisitionTimePeriodLastElapsedTicks));
            acquisitionTimePeriodLastElapsedTicks = stopwatch.ElapsedTicks;

            if (now.Subtract(lastMeasureTakenDateTime).TotalMilliseconds > measuringPeriodMs)
            {
                timeStamp = e.Header.Timestamp;
                processingWaitHandle.Set();

                lastMeasureTakenDateTime = now;
            }
            else
            {
                closedLoopLatencyMsList.Add(double.NaN);
            }

            if (processingStartTimeSpan.TotalSeconds >= processingDurationSec)
            {
                isCollectingData = false;
                BeginInvoke((MethodInvoker)(() => TerminateAcquisition()));
            }
        }
        private void BioCam_DataStreamingError(object sender, EventArgs e)
        {
            ReportLog("An error has occurred during acquisition.");
            TerminateAcquisition();
        }
        private void MeaPlate_ChipLanderStatusChanged(object sender, ChipLanderStatusChangedEventArgs e)
        {
            ReportLog($"Chip-Lander status changed | Connected Well Index: {e.ConnectedWellIdx} | Available Well Indexes {string.Join(",", e.AvailableWellIdxs.Select(x => x.ToString()))}.");
        }
        private void BioCam_DataLossAsync(object sender, DataLossEventArgs e)
        {
            ReportLog($"{e.Counter} Data loss occurred.");
        }
        #endregion

        #region GUI Events Handlers
        private async void StartButton_Click(object sender, EventArgs e)
        {
            if (isStreaming)
            {
                ReportLog("The BioCAM is already acquiring data.");
                return;
            }

            try
            {
                bool success = false;
                await Task.Run(() =>
                {
                    TakeBioCamControl();
                    ReportTime(TimeSpan.Zero);
                    success = StartAcquisition();
                });

                if (success)
                {
                    reportCll = reportSelectionComboBox.SelectedIndex == 1;

                    ReportLog("Warming up ...");
                    SetSettingControlsEnablingStatus(false);
                }
                else
                {
                    ReportLog("The process has aborted.");
                }
            }
            catch (Exception ex)
            {
                ReportLog(ex.Message);
            }
        }
        private void StopButton_Click(object sender, EventArgs e)
        {
            if (!isStreaming)
            {
                ReportLog("The BioCAM has not started.");
                return;
            }

            try
            {
                StopAcquisition();
                ReleaseBioCamControl();
            }
            catch (Exception ex)
            {
                ReportLog(ex.Message);
            }
        }
        private void ReportSelectionComboBox_SelectedIndexChanged(object sender, EventArgs e)
        {
            cllGroupBox.Enabled = reportSelectionComboBox.SelectedIndex == 1;
        }
        #endregion

        #endregion
    }
}
