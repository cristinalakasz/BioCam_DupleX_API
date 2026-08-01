namespace _3Brain.BioCamCL
{
    partial class MainForm
    {
        /// <summary>
        /// Required designer variable.
        /// </summary>
        private System.ComponentModel.IContainer components = null;

        /// <summary>
        /// Clean up any resources being used.
        /// </summary>
        /// <param name="disposing">true if managed resources should be disposed; otherwise, false.</param>
        protected override void Dispose(bool disposing)
        {
            if (disposing && (components != null))
            {
                components.Dispose();
            }
            base.Dispose(disposing);
        }

        #region Windows Form Designer generated code

        /// <summary>
        /// Required method for Designer support - do not modify
        /// the contents of this method with the code editor.
        /// </summary>
        private void InitializeComponent()
        {
            this.startButton = new System.Windows.Forms.Button();
            this.stopButton = new System.Windows.Forms.Button();
            this.settingsPanel = new System.Windows.Forms.Panel();
            this.cllGroupBox = new System.Windows.Forms.GroupBox();
            this.cllMeasuringPeriodMsNumericUpDown = new System.Windows.Forms.NumericUpDown();
            this.cllNumEndPointsNumericUpDown = new System.Windows.Forms.NumericUpDown();
            this.cllNumEndPointsLabel = new System.Windows.Forms.Label();
            this.cllMeasuringPeriodMsLabel = new System.Windows.Forms.Label();
            this.reportSelectionLabel = new System.Windows.Forms.Label();
            this.reportSelectionComboBox = new System.Windows.Forms.ComboBox();
            this.processingDurationLabel = new System.Windows.Forms.Label();
            this.processingDurationMinNumericUpDown = new System.Windows.Forms.NumericUpDown();
            this.acquisitionTimePeriodLabel = new System.Windows.Forms.Label();
            this.acquisitionTimePeriodMsNumericUpDown = new System.Windows.Forms.NumericUpDown();
            this.timeLabelControl = new System.Windows.Forms.Label();
            this.mainPanel = new System.Windows.Forms.Panel();
            this.logListBox = new System.Windows.Forms.ListBox();
            this.clGroupBox = new System.Windows.Forms.GroupBox();
            this.clWellIdxNumericUpDown = new System.Windows.Forms.NumericUpDown();
            this.clWellIdxLabel = new System.Windows.Forms.Label();
            this.settingsPanel.SuspendLayout();
            this.cllGroupBox.SuspendLayout();
            ((System.ComponentModel.ISupportInitialize)(this.cllMeasuringPeriodMsNumericUpDown)).BeginInit();
            ((System.ComponentModel.ISupportInitialize)(this.cllNumEndPointsNumericUpDown)).BeginInit();
            ((System.ComponentModel.ISupportInitialize)(this.processingDurationMinNumericUpDown)).BeginInit();
            ((System.ComponentModel.ISupportInitialize)(this.acquisitionTimePeriodMsNumericUpDown)).BeginInit();
            this.mainPanel.SuspendLayout();
            this.clGroupBox.SuspendLayout();
            ((System.ComponentModel.ISupportInitialize)(this.clWellIdxNumericUpDown)).BeginInit();
            this.SuspendLayout();
            // 
            // startButton
            // 
            this.startButton.Location = new System.Drawing.Point(22, 369);
            this.startButton.Name = "startButton";
            this.startButton.Size = new System.Drawing.Size(158, 23);
            this.startButton.TabIndex = 0;
            this.startButton.Text = "Start";
            this.startButton.UseVisualStyleBackColor = true;
            this.startButton.Click += new System.EventHandler(this.StartButton_Click);
            // 
            // stopButton
            // 
            this.stopButton.Location = new System.Drawing.Point(22, 398);
            this.stopButton.Name = "stopButton";
            this.stopButton.Size = new System.Drawing.Size(158, 23);
            this.stopButton.TabIndex = 1;
            this.stopButton.Text = "Stop";
            this.stopButton.UseVisualStyleBackColor = true;
            this.stopButton.Click += new System.EventHandler(this.StopButton_Click);
            // 
            // settingsPanel
            // 
            this.settingsPanel.Controls.Add(this.clGroupBox);
            this.settingsPanel.Controls.Add(this.cllGroupBox);
            this.settingsPanel.Controls.Add(this.reportSelectionLabel);
            this.settingsPanel.Controls.Add(this.reportSelectionComboBox);
            this.settingsPanel.Controls.Add(this.processingDurationLabel);
            this.settingsPanel.Controls.Add(this.processingDurationMinNumericUpDown);
            this.settingsPanel.Controls.Add(this.acquisitionTimePeriodLabel);
            this.settingsPanel.Controls.Add(this.acquisitionTimePeriodMsNumericUpDown);
            this.settingsPanel.Controls.Add(this.timeLabelControl);
            this.settingsPanel.Controls.Add(this.startButton);
            this.settingsPanel.Controls.Add(this.stopButton);
            this.settingsPanel.Dock = System.Windows.Forms.DockStyle.Left;
            this.settingsPanel.Location = new System.Drawing.Point(0, 0);
            this.settingsPanel.Name = "settingsPanel";
            this.settingsPanel.Size = new System.Drawing.Size(209, 567);
            this.settingsPanel.TabIndex = 2;
            // 
            // cllGroupBox
            // 
            this.cllGroupBox.Controls.Add(this.cllMeasuringPeriodMsNumericUpDown);
            this.cllGroupBox.Controls.Add(this.cllNumEndPointsNumericUpDown);
            this.cllGroupBox.Controls.Add(this.cllNumEndPointsLabel);
            this.cllGroupBox.Controls.Add(this.cllMeasuringPeriodMsLabel);
            this.cllGroupBox.Location = new System.Drawing.Point(12, 160);
            this.cllGroupBox.Name = "cllGroupBox";
            this.cllGroupBox.Size = new System.Drawing.Size(177, 117);
            this.cllGroupBox.TabIndex = 13;
            this.cllGroupBox.TabStop = false;
            this.cllGroupBox.Text = "Closed-Loop Latency (CLL)";
            // 
            // cllMeasuringPeriodMsNumericUpDown
            // 
            this.cllMeasuringPeriodMsNumericUpDown.Location = new System.Drawing.Point(9, 38);
            this.cllMeasuringPeriodMsNumericUpDown.Maximum = new decimal(new int[] {
            1000,
            0,
            0,
            0});
            this.cllMeasuringPeriodMsNumericUpDown.Minimum = new decimal(new int[] {
            50,
            0,
            0,
            0});
            this.cllMeasuringPeriodMsNumericUpDown.Name = "cllMeasuringPeriodMsNumericUpDown";
            this.cllMeasuringPeriodMsNumericUpDown.Size = new System.Drawing.Size(158, 20);
            this.cllMeasuringPeriodMsNumericUpDown.TabIndex = 9;
            this.cllMeasuringPeriodMsNumericUpDown.Value = new decimal(new int[] {
            100,
            0,
            0,
            0});
            // 
            // cllNumEndPointsNumericUpDown
            // 
            this.cllNumEndPointsNumericUpDown.Location = new System.Drawing.Point(9, 84);
            this.cllNumEndPointsNumericUpDown.Maximum = new decimal(new int[] {
            1000,
            0,
            0,
            0});
            this.cllNumEndPointsNumericUpDown.Minimum = new decimal(new int[] {
            2,
            0,
            0,
            0});
            this.cllNumEndPointsNumericUpDown.Name = "cllNumEndPointsNumericUpDown";
            this.cllNumEndPointsNumericUpDown.Size = new System.Drawing.Size(158, 20);
            this.cllNumEndPointsNumericUpDown.TabIndex = 5;
            this.cllNumEndPointsNumericUpDown.Value = new decimal(new int[] {
            2,
            0,
            0,
            0});
            // 
            // cllNumEndPointsLabel
            // 
            this.cllNumEndPointsLabel.AutoSize = true;
            this.cllNumEndPointsLabel.Location = new System.Drawing.Point(9, 68);
            this.cllNumEndPointsLabel.Name = "cllNumEndPointsLabel";
            this.cllNumEndPointsLabel.Size = new System.Drawing.Size(158, 13);
            this.cllNumEndPointsLabel.TabIndex = 6;
            this.cllNumEndPointsLabel.Text = "# of Total Stimulation EndPoints";
            // 
            // cllMeasuringPeriodMsLabel
            // 
            this.cllMeasuringPeriodMsLabel.AutoSize = true;
            this.cllMeasuringPeriodMsLabel.Location = new System.Drawing.Point(6, 22);
            this.cllMeasuringPeriodMsLabel.Name = "cllMeasuringPeriodMsLabel";
            this.cllMeasuringPeriodMsLabel.Size = new System.Drawing.Size(111, 13);
            this.cllMeasuringPeriodMsLabel.TabIndex = 10;
            this.cllMeasuringPeriodMsLabel.Text = "Measuring Period [ms]";
            // 
            // reportSelectionLabel
            // 
            this.reportSelectionLabel.AutoSize = true;
            this.reportSelectionLabel.Location = new System.Drawing.Point(22, 108);
            this.reportSelectionLabel.Name = "reportSelectionLabel";
            this.reportSelectionLabel.Size = new System.Drawing.Size(39, 13);
            this.reportSelectionLabel.TabIndex = 12;
            this.reportSelectionLabel.Text = "Report";
            // 
            // reportSelectionComboBox
            // 
            this.reportSelectionComboBox.DropDownStyle = System.Windows.Forms.ComboBoxStyle.DropDownList;
            this.reportSelectionComboBox.FormattingEnabled = true;
            this.reportSelectionComboBox.Items.AddRange(new object[] {
            "ATP",
            "ATP & CLL"});
            this.reportSelectionComboBox.Location = new System.Drawing.Point(22, 124);
            this.reportSelectionComboBox.Name = "reportSelectionComboBox";
            this.reportSelectionComboBox.Size = new System.Drawing.Size(164, 21);
            this.reportSelectionComboBox.TabIndex = 11;
            this.reportSelectionComboBox.SelectedIndexChanged += new System.EventHandler(this.ReportSelectionComboBox_SelectedIndexChanged);
            // 
            // processingDurationLabel
            // 
            this.processingDurationLabel.AutoSize = true;
            this.processingDurationLabel.Location = new System.Drawing.Point(22, 18);
            this.processingDurationLabel.Name = "processingDurationLabel";
            this.processingDurationLabel.Size = new System.Drawing.Size(127, 13);
            this.processingDurationLabel.TabIndex = 8;
            this.processingDurationLabel.Text = "Processing Duration [min]";
            // 
            // processingDurationMinNumericUpDown
            // 
            this.processingDurationMinNumericUpDown.Location = new System.Drawing.Point(22, 34);
            this.processingDurationMinNumericUpDown.Maximum = new decimal(new int[] {
            60,
            0,
            0,
            0});
            this.processingDurationMinNumericUpDown.Minimum = new decimal(new int[] {
            1,
            0,
            0,
            0});
            this.processingDurationMinNumericUpDown.Name = "processingDurationMinNumericUpDown";
            this.processingDurationMinNumericUpDown.Size = new System.Drawing.Size(164, 20);
            this.processingDurationMinNumericUpDown.TabIndex = 7;
            this.processingDurationMinNumericUpDown.Value = new decimal(new int[] {
            2,
            0,
            0,
            0});
            // 
            // acquisitionTimePeriodLabel
            // 
            this.acquisitionTimePeriodLabel.AutoSize = true;
            this.acquisitionTimePeriodLabel.Location = new System.Drawing.Point(19, 62);
            this.acquisitionTimePeriodLabel.Name = "acquisitionTimePeriodLabel";
            this.acquisitionTimePeriodLabel.Size = new System.Drawing.Size(169, 13);
            this.acquisitionTimePeriodLabel.TabIndex = 4;
            this.acquisitionTimePeriodLabel.Text = "Acquisition Time Period (ATP) [ms]";
            // 
            // acquisitionTimePeriodMsNumericUpDown
            // 
            this.acquisitionTimePeriodMsNumericUpDown.Location = new System.Drawing.Point(22, 78);
            this.acquisitionTimePeriodMsNumericUpDown.Minimum = new decimal(new int[] {
            1,
            0,
            0,
            0});
            this.acquisitionTimePeriodMsNumericUpDown.Name = "acquisitionTimePeriodMsNumericUpDown";
            this.acquisitionTimePeriodMsNumericUpDown.Size = new System.Drawing.Size(164, 20);
            this.acquisitionTimePeriodMsNumericUpDown.TabIndex = 3;
            this.acquisitionTimePeriodMsNumericUpDown.Value = new decimal(new int[] {
            2,
            0,
            0,
            0});
            // 
            // timeLabelControl
            // 
            this.timeLabelControl.AutoSize = true;
            this.timeLabelControl.Location = new System.Drawing.Point(78, 433);
            this.timeLabelControl.Name = "timeLabelControl";
            this.timeLabelControl.Size = new System.Drawing.Size(49, 13);
            this.timeLabelControl.TabIndex = 2;
            this.timeLabelControl.Text = "00:00:00";
            // 
            // mainPanel
            // 
            this.mainPanel.Controls.Add(this.logListBox);
            this.mainPanel.Dock = System.Windows.Forms.DockStyle.Fill;
            this.mainPanel.Location = new System.Drawing.Point(209, 0);
            this.mainPanel.Name = "mainPanel";
            this.mainPanel.Size = new System.Drawing.Size(626, 567);
            this.mainPanel.TabIndex = 3;
            // 
            // logListBox
            // 
            this.logListBox.Dock = System.Windows.Forms.DockStyle.Fill;
            this.logListBox.FormattingEnabled = true;
            this.logListBox.Location = new System.Drawing.Point(0, 0);
            this.logListBox.Name = "logListBox";
            this.logListBox.Size = new System.Drawing.Size(626, 567);
            this.logListBox.TabIndex = 0;
            // 
            // clGroupBox
            // 
            this.clGroupBox.Controls.Add(this.clWellIdxNumericUpDown);
            this.clGroupBox.Controls.Add(this.clWellIdxLabel);
            this.clGroupBox.Location = new System.Drawing.Point(12, 283);
            this.clGroupBox.Name = "clGroupBox";
            this.clGroupBox.Size = new System.Drawing.Size(177, 69);
            this.clGroupBox.TabIndex = 14;
            this.clGroupBox.TabStop = false;
            this.clGroupBox.Text = "Chip-Lander";
            // 
            // clWellIdxNumericUpDown
            // 
            this.clWellIdxNumericUpDown.Location = new System.Drawing.Point(9, 38);
            this.clWellIdxNumericUpDown.Maximum = new decimal(new int[] {
            3,
            0,
            0,
            0});
            this.clWellIdxNumericUpDown.Minimum = new decimal(new int[] {
            1,
            0,
            0,
            -2147483648});
            this.clWellIdxNumericUpDown.Name = "clWellIdxNumericUpDown";
            this.clWellIdxNumericUpDown.Size = new System.Drawing.Size(158, 20);
            this.clWellIdxNumericUpDown.TabIndex = 9;
            // 
            // clWellIdxLabel
            // 
            this.clWellIdxLabel.AutoSize = true;
            this.clWellIdxLabel.Location = new System.Drawing.Point(10, 22);
            this.clWellIdxLabel.Name = "clWellIdxLabel";
            this.clWellIdxLabel.Size = new System.Drawing.Size(112, 13);
            this.clWellIdxLabel.TabIndex = 10;
            this.clWellIdxLabel.Text = "Connected Well Index";
            // 
            // MainForm
            // 
            this.AutoScaleDimensions = new System.Drawing.SizeF(6F, 13F);
            this.AutoScaleMode = System.Windows.Forms.AutoScaleMode.Font;
            this.ClientSize = new System.Drawing.Size(835, 567);
            this.Controls.Add(this.mainPanel);
            this.Controls.Add(this.settingsPanel);
            this.MaximizeBox = false;
            this.MinimizeBox = false;
            this.Name = "MainForm";
            this.ShowIcon = false;
            this.Text = "BioCam Closed-Loop Demo";
            this.settingsPanel.ResumeLayout(false);
            this.settingsPanel.PerformLayout();
            this.cllGroupBox.ResumeLayout(false);
            this.cllGroupBox.PerformLayout();
            ((System.ComponentModel.ISupportInitialize)(this.cllMeasuringPeriodMsNumericUpDown)).EndInit();
            ((System.ComponentModel.ISupportInitialize)(this.cllNumEndPointsNumericUpDown)).EndInit();
            ((System.ComponentModel.ISupportInitialize)(this.processingDurationMinNumericUpDown)).EndInit();
            ((System.ComponentModel.ISupportInitialize)(this.acquisitionTimePeriodMsNumericUpDown)).EndInit();
            this.mainPanel.ResumeLayout(false);
            this.clGroupBox.ResumeLayout(false);
            this.clGroupBox.PerformLayout();
            ((System.ComponentModel.ISupportInitialize)(this.clWellIdxNumericUpDown)).EndInit();
            this.ResumeLayout(false);

        }

        #endregion

        private System.Windows.Forms.Button startButton;
        private System.Windows.Forms.Button stopButton;
        private System.Windows.Forms.Panel settingsPanel;
        private System.Windows.Forms.Panel mainPanel;
        private System.Windows.Forms.ListBox logListBox;
        private System.Windows.Forms.Label timeLabelControl;
        private System.Windows.Forms.NumericUpDown acquisitionTimePeriodMsNumericUpDown;
        private System.Windows.Forms.Label acquisitionTimePeriodLabel;
        private System.Windows.Forms.Label cllNumEndPointsLabel;
        private System.Windows.Forms.NumericUpDown cllNumEndPointsNumericUpDown;
        private System.Windows.Forms.Label processingDurationLabel;
        private System.Windows.Forms.NumericUpDown processingDurationMinNumericUpDown;
        private System.Windows.Forms.Label cllMeasuringPeriodMsLabel;
        private System.Windows.Forms.NumericUpDown cllMeasuringPeriodMsNumericUpDown;
        private System.Windows.Forms.Label reportSelectionLabel;
        private System.Windows.Forms.ComboBox reportSelectionComboBox;
        private System.Windows.Forms.GroupBox cllGroupBox;
        private System.Windows.Forms.GroupBox clGroupBox;
        private System.Windows.Forms.NumericUpDown clWellIdxNumericUpDown;
        private System.Windows.Forms.Label clWellIdxLabel;
    }
}

